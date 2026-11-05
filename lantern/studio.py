"""Avatar studio (T4a): design Lantern's face in words, drawn by Muse Image.

A design starts as one canonical preview image (a few cents), made from the
user's brief plus a style. Accepting it renders the remaining expressions
anchored on that preview (as scripts/make_avatar.py does) and sets it active.

Designs live in Lantern's own folder (`~/.lantern/avatar/<id>/`, never the
repo): the preview first, then the full expression sheet, plus a `meta.json`
with the brief and style and an `active` file naming the design on screen.
Switching back to a finished design needs no model call.
"""
from __future__ import annotations

import json
import re
import secrets
import time
from pathlib import Path

from . import avatar as avatar_mod
from . import models
from .scrubber import DEFAULT as SCRUBBER

BRIEF_MAX = 600
BRIEF_EXAMPLE = (
    "3D animated movie portrait of a sleek personal AI assistant, bright cyan eyes, "
    "confident smile, short white-silver hair with neon cyan highlights, dark gray "
    "futuristic jacket, soft rim lighting, minimalist dark studio background, clean "
    "profile picture."
)
STYLES = {
    "3d": "stylized 3D animated-film look, soft studio lighting",
    "flat": "flat vector illustration, bold simple shapes, matte colours",
    "pixel": "retro pixel art, crisp square pixels, limited palette",
    "neon": "neon glow line art on a dark background, luminous edges",
}
ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
ACTIVE_FILE = "active"
META_FILE = "meta.json"


class StudioError(ValueError):
    """The brief, style or design id was not usable."""


def validate_brief(brief: object) -> str:
    """Plain text, at most 600 characters, secrets scrubbed. Raises StudioError."""
    if not isinstance(brief, str) or not brief.strip():
        raise StudioError("describe the avatar in a few words first")
    if len(brief.strip()) > BRIEF_MAX:
        raise StudioError(f"the brief is too long (at most {BRIEF_MAX} characters)")
    return SCRUBBER.scrub(brief.strip()).text


def validate_style(style: object) -> str:
    """One of 3d | flat | pixel | neon. Raises StudioError."""
    if not isinstance(style, str) or style not in STYLES:
        raise StudioError(f"style must be one of {', '.join(sorted(STYLES))}")
    return style


def design_brief(brief: str, style: str) -> str:
    """The brief plus the style, for the canonical portrait prompt."""
    return f"{brief} {STYLES[style]}."


def new_id() -> str:
    """A fresh design id (hex, fits the ID pattern)."""
    return secrets.token_hex(6)


class StudioStore:
    """Designs in Lantern's own folder. Files are images and JSON only."""

    def __init__(self, dir: str | Path):
        self.dir = Path(dir).expanduser()

    def design_dir(self, design_id: str) -> Path:
        """The folder for one design. Raises StudioError on a bad id."""
        if not isinstance(design_id, str) or not ID.match(design_id):
            raise StudioError(f"no design called {design_id!r}")
        return self.dir / design_id

    def save_meta(self, design_id: str, meta: dict) -> None:
        folder = self.design_dir(design_id)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / META_FILE).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def get_meta(self, design_id: str) -> dict:
        """The design's brief/style/status. Raises KeyError if unknown."""
        path = self.design_dir(design_id) / META_FILE
        if not path.exists():
            raise KeyError(f"no design called {design_id!r}")
        return json.loads(path.read_text(encoding="utf-8"))

    def list_designs(self) -> list[dict]:
        """Every design with a meta file, newest first. Broken ones are skipped."""
        out = []
        if self.dir.exists():
            for path in sorted(self.dir.glob(f"*/{META_FILE}")):
                try:
                    meta = json.loads(path.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    continue
                if not isinstance(meta, dict) or "id" not in meta:
                    continue
                out.append(meta)
        out.sort(key=lambda m: m.get("created_at", 0), reverse=True)
        active = self.active_id()
        for meta in out:
            meta["active"] = meta["id"] == active
        return out

    def set_active(self, design_id: str) -> None:
        """Put this finished design on screen. Raises KeyError if unknown."""
        self.get_meta(design_id)
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / ACTIVE_FILE).write_text(design_id, encoding="utf-8")

    def active_id(self) -> str | None:
        """The design on screen, or None."""
        path = self.dir / ACTIVE_FILE
        if not path.exists():
            return None
        design_id = path.read_text(encoding="utf-8").strip()
        return design_id if (self.dir / design_id / META_FILE).exists() else None

    def file_path(self, design_id: str, expression: str) -> Path:
        """The image for one expression. Raises KeyError on a bad name or missing file."""
        folder = self.design_dir(design_id)
        if expression not in avatar_mod.EXPRESSIONS:
            raise KeyError(f"no expression called {expression!r}")
        for suffix in (".webp", ".png"):
            path = (folder / f"{expression}{suffix}").resolve()
            if path.parent == folder.resolve() and path.exists():
                return path
        raise KeyError(f"no {expression!r} image for design {design_id!r}")

    def manifest(self, api_prefix: str = "/avatar/file") -> dict:
        """The active avatar for the desktop: per-expression file URLs, plus the gallery."""
        designs = self.list_designs()
        active = self.active_id()
        expressions: dict[str, dict] = {}
        if active:
            for name in avatar_mod.EXPRESSIONS:
                try:
                    self.file_path(active, name)
                except KeyError:
                    continue  # no image on disk for this expression: skip it
                expressions[name] = {"file": f"{api_prefix}/{active}/{name}"}
        return {"active": active, "expressions": expressions, "designs": designs}


def preview_cost_usd() -> float:
    """One canonical image."""
    return models.image_cost(1)


def finish_cost_usd() -> float:
    """The remaining expressions after the preview."""
    return models.image_cost(len(avatar_mod.EXPRESSIONS) - 1)


def finish_summary(style: str, n_images: int) -> str:
    """The accept approval's summary, with the estimated cost."""
    return (f"Finish this avatar design ({style}): {n_images} more expressions anchored on the preview "
            f"(7 images, about ${models.image_cost(len(avatar_mod.EXPRESSIONS)):.2f}).")


def new_meta(design_id: str, brief: str, style: str, canonical_response: str, cost_usd: float) -> dict:
    """The meta.json for a fresh preview."""
    return {"id": design_id, "brief": brief, "style": style, "status": "preview",
            "canonical_response": canonical_response, "cost_usd": cost_usd, "created_at": time.time()}
