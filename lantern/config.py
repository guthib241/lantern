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
    model: str = models.SPARK
    base_url: str = models.BASE_URL
    db_path: str = str(HOME / "memory.db")
    zones_file: str = str(HOME / "zones.json")
    audit_path: str = str(HOME / "audit.db")
    # forms (M7): computer use develops on Spark; set forms_model to muse-wonder-1.0 for final runs
    forms_model: str = models.SPARK
    forms_protocol: str = "native"        # native computer tool | function (cookbook harness)
    forms_coords: str = ""                # "" = protocol default; pixel | normalized
    forms_headless: bool = True           # False on the laptop to watch the browser work
    forms_domains: list[str] = field(default_factory=list)  # sites (besides localhost/demo) Lantern may fill
    chromium_path: str = ""
    profile_path: str = str(HOME / "profile.json")
    browser_profile_dir: str = str(HOME / "browser-profile")
    media_dir: str = str(HOME / "media")
    envmap_path: str = str(HOME / "envmap.db")
    widgets_dir: str = str(HOME / "widgets")
    mcp_config: str = str(HOME / "mcp.json")


PATH_FIELDS = ("db_path", "zones_file", "audit_path", "profile_path", "browser_profile_dir", "media_dir",
               "envmap_path", "widgets_dir", "mcp_config")


def _flag(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def load() -> Config:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    cfg = Config()
    path = HOME / "config.json"
    if path.exists():
        data = json.loads(path.read_text())
        for k, v in data.items():
            if hasattr(cfg, k) and k not in PATH_FIELDS:
                setattr(cfg, k, v)
    if os.environ.get("LANTERN_ROOTS"):
        cfg.roots = [r for r in os.environ["LANTERN_ROOTS"].split(os.pathsep) if r]
    cfg.persona = os.environ.get("LANTERN_PERSONA", cfg.persona)
    cfg.model = os.environ.get("LANTERN_MODEL", cfg.model)
    cfg.forms_model = os.environ.get("LANTERN_FORMS_MODEL", cfg.forms_model)
    cfg.forms_protocol = os.environ.get("LANTERN_CUA_PROTOCOL", cfg.forms_protocol)
    cfg.forms_coords = os.environ.get("LANTERN_CUA_COORDS", cfg.forms_coords)
    if os.environ.get("LANTERN_FORMS_HEADLESS"):
        cfg.forms_headless = _flag(os.environ["LANTERN_FORMS_HEADLESS"])
    if os.environ.get("LANTERN_FORMS_DOMAINS"):
        cfg.forms_domains = [d.strip() for d in os.environ["LANTERN_FORMS_DOMAINS"].split(",") if d.strip()]
    cfg.chromium_path = os.environ.get("LANTERN_CHROMIUM", cfg.chromium_path)
    return cfg
