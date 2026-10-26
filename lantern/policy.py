"""Privacy zones: the only module allowed to touch the user's files.

Every path the agent asks about gets a zone. The longest matching rule wins,
and a few names are always hidden whatever the rules say.

  hidden    not even listed. The model never learns these exist.
  private   listed, but contents are only for the local Glimmer lane (router.py).
            The cloud model gets a short description instead of the text.
  open      readable; anything sent out is scrubbed (scrubber.py).
  writable  open + Lantern may create or move files here (with approval).

Anything outside every rule is out of bounds (treated as hidden).

Rules live in ~/.lantern/zones.json:
  {"zones": [{"path": "~/Documents", "zone": "writable"},
             {"path": "~/Private", "zone": "private"}],
   "hide_sensitive_names": false}
If there is no zones.json, each configured root becomes a writable zone.

Paths are resolved (symlinks followed) before matching, so a link inside an
open folder that points into a hidden one is treated as hidden.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

HIDDEN, PRIVATE, OPEN, WRITABLE = "hidden", "private", "open", "writable"
ZONES = (HIDDEN, PRIVATE, OPEN, WRITABLE)

# Always hidden, wherever they appear in a path. Code, not a prompt, enforces this.
ALWAYS_HIDDEN_NAMES = {
    ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".password-store",
    ".netrc", ".pgpass", ".git-credentials", ".npmrc", ".pypirc",
    ".env", "keychains", "login data", "key4.db", "logins.json",
    ".lantern",  # Lantern's own memory, audit log and keys
}
# Folders hidden by default when they sit under the home directory.
DEFAULT_HIDDEN_PATHS = [
    "~/.ssh", "~/.gnupg", "~/.aws", "~/.config/google-chrome", "~/.config/chromium",
    "~/.mozilla", "~/Library/Keychains", "~/Library/Application Support/Google/Chrome",
    "~/AppData/Local/Google/Chrome", "~/AppData/Roaming/Mozilla",
]
# Opt-in: top-level folder names that look like vaults.
SENSITIVE_WORDS = ("password", "passwords", "bank", "banking")


class PolicyError(Exception):
    """The path is not allowed for this action. Message is safe to show the model."""


@dataclass(frozen=True)
class Rule:
    path: Path
    zone: str

    @classmethod
    def make(cls, path: str | Path, zone: str) -> "Rule":
        if zone not in ZONES:
            raise ValueError(f"unknown zone {zone!r}; use one of {ZONES}")
        return cls(_resolve(path), zone)


def _resolve(path: str | Path) -> Path:
    return Path(os.path.expanduser(str(path))).resolve()


def _under(p: Path, base: Path) -> bool:
    return p == base or base in p.parents


class Policy:
    def __init__(self, rules: list[Rule], hide_sensitive_names: bool = False):
        if not any(r.zone != HIDDEN for r in rules):
            raise ValueError("Lantern needs at least one folder that is not hidden")
        self.rules = sorted(rules, key=lambda r: len(r.path.parts), reverse=True)
        self.hide_sensitive_names = hide_sensitive_names

    # ---- construction --------------------------------------------------------
    @classmethod
    def from_roots(cls, roots: list[str | Path]) -> "Policy":
        rules = [Rule.make(r, WRITABLE) for r in roots]
        rules += [Rule.make(h, HIDDEN) for h in DEFAULT_HIDDEN_PATHS]
        return cls(rules)

    @classmethod
    def load(cls, zones_file: str | Path, roots: list[str | Path]) -> "Policy":
        """Read zones.json; fall back to roots-as-writable when the file is missing."""
        zf = Path(zones_file).expanduser()
        if not zf.exists():
            return cls.from_roots(roots)
        data = json.loads(zf.read_text())
        rules = [Rule.make(z["path"], z["zone"]) for z in data.get("zones", [])]
        rules += [Rule.make(h, HIDDEN) for h in DEFAULT_HIDDEN_PATHS]
        return cls(rules, bool(data.get("hide_sensitive_names", False)))

    def save(self, zones_file: str | Path) -> None:
        default_hidden = {_resolve(h) for h in DEFAULT_HIDDEN_PATHS}
        data = {
            "zones": [{"path": str(r.path), "zone": r.zone}
                      for r in sorted(self.rules, key=lambda r: str(r.path))
                      if not (r.zone == HIDDEN and r.path in default_hidden)],
            "hide_sensitive_names": self.hide_sensitive_names,
        }
        zf = Path(zones_file).expanduser()
        zf.parent.mkdir(parents=True, exist_ok=True)
        zf.write_text(json.dumps(data, indent=2))

    def set_zone(self, path: str | Path, zone: str) -> None:
        new = Rule.make(path, zone)
        rules = [r for r in self.rules if r.path != new.path] + [new]
        self.__init__(rules, self.hide_sensitive_names)

    def describe(self) -> list[dict]:
        return [{"path": str(r.path), "zone": r.zone} for r in self.rules]

    @property
    def roots(self) -> list[Path]:
        """Top-most visible folders (what the model may start from)."""
        visible = [r.path for r in self.rules if r.zone != HIDDEN]
        return sorted({p for p in visible if not any(o != p and _under(p, o) for o in visible)}, key=str)

    # ---- classification -----------------------------------------------------
    def zone_of(self, path: str | Path) -> str:
        p = _resolve(path)
        if any(part.lower() in ALWAYS_HIDDEN_NAMES or part.lower().startswith(".env.") for part in p.parts):
            return HIDDEN
        for rule in self.rules:  # longest first
            if _under(p, rule.path):
                if rule.zone != HIDDEN and self.hide_sensitive_names and self._sensitive(p, rule.path):
                    return HIDDEN
                return rule.zone
        return HIDDEN

    def _sensitive(self, p: Path, base: Path) -> bool:
        rel = p.relative_to(base).parts
        return bool(rel) and any(w in rel[0].lower() for w in SENSITIVE_WORDS)

    def resolve(self, path: str | Path, default_root: Path | None = None) -> Path:
        """Turn a model-supplied path into an absolute, resolved path."""
        p = Path(os.path.expanduser(str(path)))
        if not p.is_absolute():
            roots = self.roots
            p = (default_root or roots[0]) / p
        return p.resolve()

    def _deny(self, path: str | Path) -> PolicyError:
        # Same message for hidden and outside, so hidden paths are not revealed.
        return PolicyError(
            f"'{path}' is outside the folders I'm allowed to touch: "
            + ", ".join(str(r) for r in self.roots)
        )

    def check_visible(self, path: str | Path) -> tuple[Path, str]:
        p = self.resolve(path)
        zone = self.zone_of(p)
        if zone == HIDDEN:
            raise self._deny(path)
        return p, zone

    def check_writable(self, path: str | Path) -> Path:
        p, zone = self.check_visible(path)
        if zone != WRITABLE:
            raise PolicyError(f"'{p}' is in a {zone} zone; I can only change files in writable zones")
        return p

    # ---- file access (the only file I/O in Lantern) -------------------------
    def list_dir(self, path: str | Path) -> list[Path]:
        p, _ = self.check_visible(path)
        if not p.is_dir():
            raise PolicyError(f"{p} is not a folder")
        return [c for c in sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower()))
                if self.zone_of(c) != HIDDEN]

    def walk(self, root: str | Path, skip_dirs: set[str] = frozenset()) -> Iterator[tuple[Path, str]]:
        """Yield (file, zone) for every visible file under root."""
        base, _ = self.check_visible(root)
        for dirpath, dirnames, filenames in os.walk(base):
            d = Path(dirpath)
            dirnames[:] = [n for n in dirnames if n not in skip_dirs and not n.startswith(".")
                           and self.zone_of(d / n) != HIDDEN]
            for name in filenames:
                f = d / name
                zone = self.zone_of(f)
                if zone != HIDDEN:
                    yield f, zone

    def walk_dirs(self, root: str | Path, skip_dirs: set[str] = frozenset(),
                  max_depth: int = 6) -> Iterator[tuple[Path, list[str], list[str]]]:
        """Yield (folder, file names, subfolder names) for visible folders under root.
        Subfolder names include dot-folders like .git/.venv (so callers can spot them), but the
        walk never descends into dot-folders, skip_dirs, or hidden zones."""
        base, _ = self.check_visible(root)
        start = len(base.parts)
        for dirpath, dirnames, filenames in os.walk(base):
            d = Path(dirpath)
            visible = [n for n in dirnames if self.zone_of(d / n) != HIDDEN]
            files = [n for n in filenames if self.zone_of(d / n) != HIDDEN]
            yield d, files, visible
            depth = len(d.parts) - start
            dirnames[:] = [] if depth >= max_depth else [
                n for n in visible if n not in skip_dirs and not n.startswith(".")]

    def stat(self, path: str | Path) -> os.stat_result:
        p, _ = self.check_visible(path)
        return p.stat()

    def read_bytes(self, path: str | Path, *, allow_private: bool = False) -> bytes:
        p, zone = self.check_visible(path)
        if zone == PRIVATE and not allow_private:
            raise PolicyError(f"'{p}' is private; only the local model may read it")
        return p.read_bytes()

    def read_text(self, path: str | Path, *, allow_private: bool = False, limit: int | None = None) -> str:
        p, zone = self.check_visible(path)
        if zone == PRIVATE and not allow_private:
            raise PolicyError(f"'{p}' is private; only the local model may read it")
        with p.open("r", errors="replace") as f:
            return f.read(limit) if limit else f.read()

    def write_text(self, path: str | Path, content: str) -> Path:
        p = self.check_writable(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return p

    def write_bytes(self, path: str | Path, content: bytes) -> Path:
        p = self.check_writable(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return p

    def nested_zones(self, folder: str | Path) -> list[Rule]:
        """Zone rules strictly inside `folder` that differ from its own zone."""
        f = _resolve(folder)
        own = self.zone_of(f)
        return [r for r in self.rules if r.path != f and _under(r.path, f) and r.zone != own]

    def move(self, src: str | Path, dst: str | Path) -> tuple[Path, Path]:
        s = self.check_writable(src)
        d = self.check_writable(dst)
        if s.is_dir() and (inner := self.nested_zones(s)):
            # Zones are paths: moving the folder would carry a private or hidden folder out of its zone.
            raise PolicyError(f"'{s}' contains a {inner[0].zone} folder ({inner[0].path}); move files, not this folder")
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(s), str(d))
        return s, d

    def erase_lantern_data(self, target: str | Path, lantern_home: str | Path) -> int:
        """"Forget everything": overwrite and delete Lantern's OWN files (voice notes, screenshots,
        outbox, browser profile) under its home folder. Never touches anything in a user zone."""
        home = _resolve(lantern_home)
        t = _resolve(target)
        if not _under(t, home) or t == home or self.zone_of(t) != HIDDEN:
            raise PolicyError(f"refusing to erase {t}: not Lantern's own data")
        if not t.exists():
            return 0
        files = [t] if t.is_file() else [Path(d) / f for d, _, fs in os.walk(t) for f in fs]
        for f in files:
            if f.is_symlink():
                f.unlink()
                continue
            size = f.stat().st_size
            with f.open("r+b") as fh:  # best effort overwrite before unlinking
                fh.write(b"\0" * min(size, 50_000_000))
            f.unlink()
        if t.is_dir():
            shutil.rmtree(t, ignore_errors=True)
        return len(files)

    def exists(self, path: str | Path) -> bool:
        try:
            p, _ = self.check_visible(path)
        except PolicyError:
            return False
        return p.exists()

    def describe_file(self, path: str | Path) -> dict:
        """A redacted description of a file, safe to send to the cloud for private files."""
        p, zone = self.check_visible(path)
        st = p.stat()
        desc = {"name": p.name, "type": (p.suffix.lstrip(".").upper() or "file"),
                "size_bytes": st.st_size, "zone": zone}
        if p.suffix.lower() in {".txt", ".md", ".csv", ".log"} and st.st_size < 5_000_000:
            with p.open("rb") as f:
                desc["lines"] = sum(1 for _ in f)
        elif p.suffix.lower() == ".pdf" and st.st_size < 20_000_000:
            pages = len(re.findall(rb"/Type\s*/Page(?![a-zA-Z])", p.read_bytes()))
            if pages:
                desc["pages"] = pages
        return desc
