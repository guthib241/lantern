"""The environment map: which projects live on this computer, what they depend on, and which
Python environments they use. Answers "which projects use torch?" from a local cache, without a
model reading any files.

Discovery walks the allowed folders through policy.py, looking for project markers
(.git, pyproject.toml, requirements*.txt, uv.lock, poetry.lock, Pipfile, setup.py,
package.json, environment.yml). Per project:
  declared   dependencies from requirements*.txt, pyproject.toml ([project], optional deps,
             Poetry), uv.lock / poetry.lock, Pipfile, package.json, environment.yml
  installed  per project virtual env (.venv / venv / env with a pyvenv.cfg), read from the
             site-packages *.dist-info folder names: no project code is executed to find out
  conda      `conda env list --json` + `conda list -p <env> --json` when conda exists
Results are cached in SQLite (~/.lantern/envmap.db), keyed by the mtimes of the marker files and
the env's site-packages folder, so a rescan only re-reads projects that changed.
Private-zone projects are listed by name only; their files are never read for the cloud.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import time
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .policy import PRIVATE, Policy, PolicyError

MARKER_FILES = ("pyproject.toml", "setup.py", "setup.cfg", "Pipfile", "uv.lock", "poetry.lock",
                "package.json", "environment.yml", "environment.yaml")
SKIP_DIRS = {"node_modules", "__pycache__", "venv", "env", "site-packages", "dist", "build", ".tox", "target"}
ENV_DIRS = (".venv", "venv", "env")
RESCAN_AFTER = 60.0  # seconds


def norm(name: str) -> str:
    """PEP 503 package-name normalization: 'Torch_Vision' -> 'torch-vision'."""
    return re.sub(r"[-_.]+", "-", name.strip()).lower()


_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(.*)$")


def parse_requirement(line: str) -> tuple[str, str] | None:
    """'torch==2.3.0 ; python_version>"3.8"  # gpu' -> ('torch', '==2.3.0')."""
    line = line.split("#", 1)[0].strip()
    if not line or line.startswith(("-", "git+", "http:", "https:", "file:", ".")):
        return None
    m = _REQ_NAME.match(line)
    if not m:
        return None
    spec = m.group(3).split(";", 1)[0].strip()
    return norm(m.group(1)), spec


@dataclass
class Env:
    path: str
    kind: str                 # venv | conda
    python: str = ""          # interpreter path (venv)
    packages: dict = field(default_factory=dict)   # normalized name -> version


@dataclass
class Project:
    path: str
    name: str
    kinds: list = field(default_factory=list)       # python | node | git
    declared: dict = field(default_factory=dict)    # normalized name -> spec ("" if unpinned)
    sources: list = field(default_factory=list)     # which files declared them
    envs: list = field(default_factory=list)        # [Env as dict]
    scripts: dict = field(default_factory=dict)     # package.json scripts
    private: bool = False

    def summary(self) -> dict:
        return {"name": self.name, "path": self.path, "kinds": self.kinds, "private": self.private,
                "declared": len(self.declared), "envs": [e["path"] for e in self.envs]}


Runner = Callable[[list[str], float], tuple[int, str]]   # (argv, timeout) -> (exit code, stdout)


def run_quiet(argv: list[str], timeout: float = 30) -> tuple[int, str]:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return r.returncode, r.stdout
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""


class EnvMap:
    def __init__(self, policy: Policy, cache_path: str | Path, *, runner: Runner = run_quiet,
                 conda: str | None = None, clock: Callable[[], float] = time.time):
        self.policy = policy
        self.runner = runner
        self.conda = conda if conda is not None else (shutil.which("conda") or "")
        self.clock = clock
        path = Path(cache_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS projects (path TEXT PRIMARY KEY, key TEXT NOT NULL, data TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS conda (path TEXT PRIMARY KEY, at REAL NOT NULL, data TEXT NOT NULL)")
        self.db.commit()
        self._projects: list[Project] = []
        self._scanned_at = 0.0
        self.parsed = 0  # projects actually re-read on the last scan (the rest came from the cache)

    # ---- discovery -----------------------------------------------------------------------
    def projects(self, refresh: bool = False) -> list[Project]:
        if refresh or not self._projects or self.clock() - self._scanned_at > RESCAN_AFTER:
            self._projects = self.scan()
            self._scanned_at = self.clock()
        return self._projects

    def scan(self) -> list[Project]:
        found: list[Project] = []
        self.parsed = 0
        for root in self.policy.roots:
            for folder, files, dirs in self.policy.walk_dirs(root, SKIP_DIRS):
                markers = [f for f in files if f in MARKER_FILES or re.match(r"requirements.*\.txt$", f)]
                if not markers and ".git" not in dirs:
                    continue
                found.append(self._project(folder, markers, dirs))
        found.sort(key=lambda p: p.path)
        return found

    def _mtime(self, path: Path) -> float:
        try:
            return self.policy.stat(path).st_mtime
        except (PolicyError, OSError):
            return 0.0

    def _project(self, folder: Path, markers: list[str], dirs: list[str]) -> Project:
        envs = [folder / d for d in ENV_DIRS if d in dirs and self.policy.exists(folder / d / "pyvenv.cfg")]
        site = [self._site_packages(e) for e in envs]
        key = json.dumps([[m, self._mtime(folder / m)] for m in sorted(markers)]
                         + [[str(s), self._mtime(s)] for s in site if s] + [".git" in dirs])
        row = self.db.execute("SELECT key, data FROM projects WHERE path = ?", (str(folder),)).fetchone()
        if row and row[0] == key:
            return Project(**json.loads(row[1]))
        self.parsed += 1
        project = self._read(folder, markers, dirs, envs)
        self.db.execute("INSERT OR REPLACE INTO projects (path, key, data) VALUES (?, ?, ?)",
                        (str(folder), key, json.dumps(asdict(project))))
        self.db.commit()
        return project

    def _read(self, folder: Path, markers: list[str], dirs: list[str], envs: list[Path]) -> Project:
        p = Project(path=str(folder), name=folder.name)
        if ".git" in dirs:
            p.kinds.append("git")
        if self.policy.zone_of(folder) == PRIVATE:
            p.private = True
            return p
        for m in markers:
            try:
                self._declared(p, folder / m, m)
            except (PolicyError, OSError, ValueError, tomllib.TOMLDecodeError, json.JSONDecodeError):
                p.sources.append(f"{m} (unreadable)")
        if any(m != "package.json" for m in markers) and "python" not in p.kinds:
            p.kinds.append("python")
        for env in envs:
            p.envs.append(asdict(self._venv(env)))
        if envs and "python" not in p.kinds:
            p.kinds.append("python")
        return p

    def _declared(self, p: Project, path: Path, marker: str) -> None:
        def add(name: str, spec: str = "") -> None:
            if name:
                p.declared.setdefault(norm(name), spec)
        if marker.startswith("requirements"):
            for line in self.policy.read_text(path, limit=500_000).splitlines():
                if (req := parse_requirement(line)):
                    add(*req)
        elif marker == "pyproject.toml":
            data = tomllib.loads(self.policy.read_text(path, limit=500_000))
            project = data.get("project", {})
            p.name = project.get("name", p.name)
            for dep in project.get("dependencies", []) + [d for ds in project.get("optional-dependencies", {}).values() for d in ds]:
                if (req := parse_requirement(dep)):
                    add(*req)
            poetry = data.get("tool", {}).get("poetry", {})
            p.name = poetry.get("name", p.name)
            for name, spec in poetry.get("dependencies", {}).items():
                if name.lower() != "python":
                    add(name, spec if isinstance(spec, str) else "")
        elif marker in ("uv.lock", "poetry.lock"):
            data = tomllib.loads(self.policy.read_text(path, limit=5_000_000))
            for pkg in data.get("package", []):
                add(pkg.get("name", ""), f"=={pkg['version']}" if pkg.get("version") else "")
        elif marker == "Pipfile":
            data = tomllib.loads(self.policy.read_text(path, limit=500_000))
            for section in ("packages", "dev-packages"):
                for name, spec in data.get(section, {}).items():
                    add(name, spec if isinstance(spec, str) and spec != "*" else "")
        elif marker == "package.json":
            data = json.loads(self.policy.read_text(path, limit=2_000_000))
            p.name = data.get("name") or p.name
            p.scripts = {k: str(v)[:200] for k, v in (data.get("scripts") or {}).items()}
            for section in ("dependencies", "devDependencies"):
                for name, spec in (data.get(section) or {}).items():
                    p.declared.setdefault(f"npm:{name}", str(spec))
            if "node" not in p.kinds:
                p.kinds.append("node")
            return
        elif marker.startswith("environment"):
            for line in self.policy.read_text(path, limit=500_000).splitlines():
                m = re.match(r"^\s*-\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*([=<>!~].*)?$", line)
                if m and m.group(1) not in ("pip",):
                    add(m.group(1), (m.group(2) or "").strip())
        p.sources.append(marker)

    def _site_packages(self, env: Path) -> Path | None:
        candidates = [env / "Lib" / "site-packages"]  # Windows
        lib = env / "lib"
        if self.policy.exists(lib):
            try:
                candidates += [c / "site-packages" for c in self.policy.list_dir(lib) if c.name.startswith("python")]
            except PolicyError:
                pass
        return next((c for c in candidates if self.policy.exists(c)), None)

    def _venv(self, env: Path) -> Env:
        python = env / ("Scripts/python.exe" if os.name == "nt" and self.policy.exists(env / "Scripts") else "bin/python")
        out = Env(path=str(env), kind="venv", python=str(python))
        site = self._site_packages(env)
        if site is None:
            return out
        for entry in self.policy.list_dir(site):
            m = re.match(r"^(.+?)-([^-]+)\.(dist-info|egg-info)$", entry.name)
            if m:
                out.packages[norm(m.group(1))] = m.group(2)
        return out

    # ---- conda -----------------------------------------------------------------------------
    def conda_envs(self, max_age: float = 3600) -> list[Env]:
        if not self.conda:
            return []
        code, out = self.runner([self.conda, "env", "list", "--json"], 30)
        if code != 0:
            return []
        envs = []
        for path in json.loads(out or "{}").get("envs", []):
            row = self.db.execute("SELECT at, data FROM conda WHERE path = ?", (path,)).fetchone()
            if row and self.clock() - row[0] < max_age:
                envs.append(Env(**json.loads(row[1])))
                continue
            code, listing = self.runner([self.conda, "list", "-p", path, "--json"], 60)
            pkgs = {norm(x["name"]): x.get("version", "") for x in json.loads(listing or "[]")} if code == 0 else {}
            env = Env(path=path, kind="conda", packages=pkgs)
            self.db.execute("INSERT OR REPLACE INTO conda (path, at, data) VALUES (?, ?, ?)",
                            (path, self.clock(), json.dumps(asdict(env))))
            self.db.commit()
            envs.append(env)
        return envs

    # ---- questions ---------------------------------------------------------------------------
    def find(self, name: str) -> list[Project]:
        q = name.strip().lower()
        return [p for p in self.projects() if q in p.name.lower() or q in Path(p.path).name.lower()]

    def get(self, path: str) -> Project | None:
        target = str(self.policy.resolve(path))
        return next((p for p in self.projects() if p.path == target), None)

    def which_use(self, package: str) -> dict:
        """Projects that declare or have installed `package` (and conda envs that have it)."""
        package = package.strip()
        key = norm(package.removeprefix("npm:"))
        names = [f"npm:{package.removeprefix('npm:')}"] if package.startswith("npm:") else [key, f"npm:{package}"]
        hits = []
        for p in self.projects():
            declared_as = next((n for n in names if n in p.declared), None)
            installed = {e["path"]: e["packages"][key] for e in p.envs if key in e["packages"]}
            if declared_as or installed:
                hits.append({"project": p.name, "path": p.path,
                             "declared": p.declared[declared_as] if declared_as else None,
                             "installed": installed or None})
        conda = [{"env": e.path, "version": e.packages[key]} for e in self.conda_envs() if key in e.packages]
        return {"package": package, "projects": hits, "conda_envs": conda,
                "scanned_projects": len(self._projects)}


# ---- running commands inside a project (allowlist) ----------------------------------------------
PYTEST_FLAG = re.compile(r"^(-q|-qq|-x|-v|-vv|-s|-l|--lf|--ff|--sw|--maxfail=\d+|--tb=(short|line|no|long|native))$")
PIP_SPEC = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9,._-]+\])?((==|>=|<=|~=|!=|>|<)[A-Za-z0-9.*+!-]+)?$")
SECRET_ENV = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|COOKIE|SESSION", re.I)
ALLOWED = "pytest [-q -x -k EXPR tests/...], python -m pytest ..., npm test, pip install <one package>"


class CommandError(ValueError):
    """The command is not on the allowlist (message is safe to show the model)."""


def plan_command(project: Project, command: str) -> dict:
    """Turn a requested command into the exact argv Lantern will run, or refuse.
    Python always runs from the project's own virtual env, never the global interpreter."""
    try:
        argv = shlex.split(command)
    except ValueError as e:
        raise CommandError(f"can't parse the command: {e}") from e
    if not argv:
        raise CommandError("empty command")
    venv = next((e for e in project.envs if e.get("kind") == "venv" and e.get("python")), None)
    root = Path(project.path)

    def needs_env(what: str) -> dict:
        if venv is None:
            raise CommandError(f"{project.name} has no virtual env (.venv/venv); I won't {what} with the global Python")
        return venv

    if argv[0] == "pytest" or argv[:3] == ["python", "-m", "pytest"] or argv[:3] == ["python3", "-m", "pytest"]:
        rest = argv[1:] if argv[0] == "pytest" else argv[3:]
        clean, i = [], 0
        while i < len(rest):
            a = rest[i]
            if a == "-k" and i + 1 < len(rest):
                clean += ["-k", rest[i + 1]]
                i += 2
                continue
            if PYTEST_FLAG.match(a):
                clean.append(a)
            elif not a.startswith("-") and (root / a).resolve().is_relative_to(root.resolve()) and ".." not in Path(a).parts:
                clean.append(a)
            else:
                raise CommandError(f"pytest argument {a!r} is not allowed")
            i += 1
        env = needs_env("run tests")
        return {"kind": "pytest", "argv": [env["python"], "-m", "pytest", *clean], "cwd": project.path,
                "env": env["path"], "display": "pytest " + " ".join(clean)}
    if argv in (["npm", "test"], ["npm", "run", "test"], ["npm", "t"]):
        if "test" not in project.scripts:
            raise CommandError(f"{project.name} has no npm test script")
        npm = shutil.which("npm") or "npm"
        return {"kind": "npm_test", "argv": [npm, "test"], "cwd": project.path, "env": "", "display": "npm test"}
    if argv[:2] == ["pip", "install"] or argv[:4] in (["python", "-m", "pip", "install"], ["python3", "-m", "pip", "install"]):
        specs = argv[2:] if argv[0] == "pip" else argv[4:]
        if len(specs) != 1 or not PIP_SPEC.match(specs[0]):
            raise CommandError("pip install takes exactly one package name (optionally ==version), no options or URLs")
        env = needs_env("install packages")
        name = norm(re.split(r"[\[=<>!~]", specs[0], maxsplit=1)[0])
        return {"kind": "pip_install", "argv": [env["python"], "-m", "pip", "install", specs[0]], "cwd": project.path,
                "env": env["path"], "package": name, "display": f"pip install {specs[0]}"}
    raise CommandError(f"not on the allowlist. Allowed: {ALLOWED}")


def safe_env(venv_path: str = "") -> dict:
    """The environment for project commands: no API keys, tokens or passwords from Lantern's own."""
    env = {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}
    if venv_path:
        bindir = str(Path(venv_path) / ("Scripts" if os.name == "nt" else "bin"))
        env["VIRTUAL_ENV"] = venv_path
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        env.pop("PYTHONHOME", None)
    return env


def run_command(argv: list[str], cwd: str, env: dict, timeout: float) -> tuple[int, str]:
    try:
        r = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return 124, out + f"\n[stopped after {timeout:.0f} s]"
    except OSError as e:
        return 127, f"couldn't start {argv[0]}: {e}"
