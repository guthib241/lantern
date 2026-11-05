"""Settings, read from ~/.lantern/config.json and environment variables.

config.json example:
{
  "roots": ["~/Documents", "~/Downloads"],
  "persona": "smug",
  "model": "muse-spark-1.3",
  "forms_model": "muse-wonder-1.0",
  "forms_domains": ["forms.example.org"]
}

The API key is only read from the MODEL_API_KEY environment variable (or a .env
file), never from config.json, so it can't end up in git by accident.
"""
from __future__ import annotations

import codecs
import io
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import models

HOME = Path(os.environ.get("LANTERN_HOME", "~/.lantern")).expanduser()


@dataclass
class Config:
    roots: list[str] = field(default_factory=lambda: [str(Path.home() / "Documents")])
    persona: str = "smug"
    skin: str = "muse"          # the avatar window's skin: muse (drawn faces), glow, or clock (T4b)
    model: str = models.SPARK
    base_url: str = models.BASE_URL
    db_path: str = str(HOME / "memory.db")
    zones_file: str = str(HOME / "zones.json")
    audit_path: str = str(HOME / "audit.db")
    # forms (M7): computer use develops on Spark; set forms_model to muse-wonder-1.0 for final runs
    forms_model: str = models.SPARK
    forms_protocol: str = "auto"          # auto (per model, models.CUA_PROTOCOL) | native | function
    forms_coords: str = ""                # "" = protocol default; pixel | normalized
    forms_headless: bool = True           # False on the laptop to watch the browser work
    forms_domains: list[str] = field(default_factory=list)  # sites (besides localhost/demo) Lantern may fill
    forms_screenshots: str = "raw"        # S0.5: raw | masked; S7: decoy (scripts/eval_privacy.py measures all three)
    chromium_path: str = ""
    profile_path: str = str(HOME / "profile.json")
    browser_profile_dir: str = str(HOME / "browser-profile")
    media_dir: str = str(HOME / "media")
    envmap_path: str = str(HOME / "envmap.db")
    widgets_dir: str = str(HOME / "widgets")
    agents_dir: str = str(HOME / "agents")
    mcp_config: str = str(HOME / "mcp.json")
    encrypt: bool = True                  # memory + audit log encrypted at rest (key in the OS keychain)
    timeout: float = 120.0                # seconds per model call (LANTERN_TIMEOUT)
    workers: int = 3                      # tasks the server runs at once (LANTERN_WORKERS; scheduler.py)
    localsend: bool = False               # receive files from phones over Wi-Fi (Setup switch or LANTERN_LOCALSEND=1)


PATH_FIELDS = ("db_path", "zones_file", "audit_path", "profile_path", "browser_profile_dir", "media_dir",
               "envmap_path", "widgets_dir", "agents_dir", "mcp_config")


SETTABLE = ("persona", "skin", "localsend")   # what the desktop's Preferences may change


def save_setting(key: str, value, home: str | Path) -> Path:
    """Write one preference into <home>/config.json (merged, written atomically). home is the folder
    that holds Lantern's memory: ~/.lantern normally, the rehearsal's own folder under --demo."""
    if key not in SETTABLE:
        raise ValueError(f"{key} can't be set from the app")
    path = Path(home).expanduser() / "config.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    data[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def _flag(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


_BOMS = ((codecs.BOM_UTF8, "utf-8-sig"), (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16"))


def read_env_file(path: str | Path) -> str:
    """A .env file as text, whatever saved it. Windows PowerShell 5.1's `echo KEY=... > .env` writes UTF-16
    with a byte-order mark, which a plain UTF-8 read turns into a crash or a missing key."""
    raw = Path(path).read_bytes()
    for bom, encoding in _BOMS:
        if raw.startswith(bom):
            return raw.decode(encoding, errors="replace")
    return raw.decode("utf-8", errors="replace")


def load_env(path: str | Path | None = None) -> bool:
    """Load the repo's .env into the environment (variables already set win). True if a file was read."""
    try:
        from dotenv import find_dotenv, load_dotenv
    except ImportError:
        return False
    found = path or find_dotenv()   # searched upward from this package, as before, not from the current folder
    if not found or not Path(found).is_file():
        return False
    load_dotenv(stream=io.StringIO(read_env_file(found)))
    return True


def load() -> Config:
    load_env()
    cfg = Config()
    path = HOME / "config.json"
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        for k, v in data.items():
            if hasattr(cfg, k) and k not in PATH_FIELDS:
                setattr(cfg, k, v)
    if os.environ.get("LANTERN_ROOTS"):
        cfg.roots = [r for r in os.environ["LANTERN_ROOTS"].split(os.pathsep) if r]
    cfg.persona = os.environ.get("LANTERN_PERSONA", cfg.persona)
    cfg.model = os.environ.get("LANTERN_MODEL", cfg.model)
    cfg.forms_model = os.environ.get("LANTERN_FORMS_MODEL", cfg.forms_model)
    cfg.forms_protocol = os.environ.get("LANTERN_CUA_PROTOCOL", cfg.forms_protocol)
    cfg.forms_screenshots = os.environ.get("LANTERN_SCREENSHOTS", cfg.forms_screenshots).strip().lower()
    if cfg.forms_screenshots not in ("raw", "masked", "decoy"):
        raise ValueError("LANTERN_SCREENSHOTS must be raw, masked or decoy")
    cfg.forms_coords = os.environ.get("LANTERN_CUA_COORDS", cfg.forms_coords)
    if os.environ.get("LANTERN_FORMS_HEADLESS"):
        cfg.forms_headless = _flag(os.environ["LANTERN_FORMS_HEADLESS"])
    if os.environ.get("LANTERN_FORMS_DOMAINS"):
        cfg.forms_domains = [d.strip() for d in os.environ["LANTERN_FORMS_DOMAINS"].split(",") if d.strip()]
    cfg.chromium_path = os.environ.get("LANTERN_CHROMIUM", cfg.chromium_path)
    if os.environ.get("LANTERN_TIMEOUT"):
        cfg.timeout = float(os.environ["LANTERN_TIMEOUT"])
    if os.environ.get("LANTERN_ENCRYPT"):
        cfg.encrypt = _flag(os.environ["LANTERN_ENCRYPT"])
    if os.environ.get("LANTERN_WORKERS", "").strip().isdigit():
        cfg.workers = int(os.environ["LANTERN_WORKERS"])
    if os.environ.get("LANTERN_LOCALSEND") not in (None, ""):
        cfg.localsend = _flag(os.environ["LANTERN_LOCALSEND"])
    return cfg
