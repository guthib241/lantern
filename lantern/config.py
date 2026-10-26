"""Settings, read from ~/.lantern/config.json and environment variables.

config.json example:
{
  "roots": ["~/Documents", "~/Downloads"],
  "persona": "smug",
  "model": "muse-spark-1.3"
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
            if hasattr(cfg, k) and k not in ("db_path", "zones_file", "audit_path"):
                setattr(cfg, k, v)
    if os.environ.get("LANTERN_ROOTS"):
        cfg.roots = [r for r in os.environ["LANTERN_ROOTS"].split(os.pathsep) if r]
    cfg.persona = os.environ.get("LANTERN_PERSONA", cfg.persona)
    cfg.model = os.environ.get("LANTERN_MODEL", cfg.model)
    return cfg
