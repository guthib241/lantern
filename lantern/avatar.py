"""Lantern's face, made by Muse Image (muse-image-1.0), never drawn by hand.

How the sheet is made (meta-model-cookbook 05_muse_image/03_anchored_generation, and
01_image_api_fundamentals for reference images):
  1. One canonical turn renders the character (or, with your own art from the MetaMuse app,
     redraws it as a clean portrait from an `input_image` reference).
  2. Every other expression is its own turn chained from that canonical turn with
     `previous_response_id`, so the character stays on-model: same design, style, framing.
  3. The image is the base64 `result` on the `image_generation_call` output item (WebP).
Square 1024x1024 via tools=[{"type": "image_generation", "size": "1024x1024"}].

Writes desktop/assets/avatar/<expression>.webp, manifest.json (for people and tools) and
manifest.js (for the desktop renderer, which can't fetch local JSON). Cost: $0.01 per image.
"""
from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import models

EXPRESSIONS: dict[str, str] = {
    "idle": "calm and content, a small confident half-smile, eyes open looking at the viewer",
    "thinking": "thinking hard: eyes glancing up and to the side, a tiny glowing spark floating near its head",
    "smug": "smug and pleased with itself: half-closed eyes, a one-sided smirk, chin slightly raised",
    "annoyed": "mildly annoyed: flat unimpressed eyes, a small frown, its glow a little dimmer",
    "celebrating": "celebrating: eyes closed in happy arcs, a wide open smile, extra bright glow, a few sparkles around it",
    "needs-approval": "politely asking for permission: big hopeful eyes on the viewer, eyebrows raised, holding up a small blank card with a check mark",
    "sleeping": "fast asleep: eyes closed, peaceful face, its glow dimmed to a soft ember, a small z shape floating beside it",
}
CHARACTER = (
    "Lantern, an original friendly AI companion character: a small paper-lantern spirit with a warm "
    "orange-amber body lit softly from inside, big expressive dark eyes, rosy cheeks and tiny arms. "
    "Stylized 3D animated-film look, soft studio lighting."
)
FRAMING = (
    "Square portrait, the character centred and filling about 70% of the frame, on a plain soft warm "
    "cream-to-peach gradient background. No text, no letters, no logos, no watermark."
)
SIZE = "1024x1024"
MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


class AvatarError(RuntimeError):
    pass


@dataclass
class Turn:
    expression: str
    prompt: str
    chained: bool                      # previous_response_id = the canonical turn
    reference: bool = False            # carries the user's own art as input_image


@dataclass
class Sheet:
    out_dir: Path
    made: dict = field(default_factory=dict)
    canonical_response: str = ""
    cost_usd: float = 0.0


def canonical_prompt(brief: str | None, from_reference: bool) -> str:
    if from_reference:
        return ("This is Lantern, my AI companion character, made in the MetaMuse app. Keep this exact "
                "character: same design, colours, proportions and art style. Redraw it as a clean portrait. "
                f"{FRAMING} Expression: {EXPRESSIONS['idle']}.")
    return f"{brief or CHARACTER} {FRAMING} Expression: {EXPRESSIONS['idle']}."


def expression_prompt(expression: str) -> str:
    return (f"The same character as in the canonical portrait: identical design, colours, style, framing and "
            f"background. Change only the expression and pose: {EXPRESSIONS[expression]}. No text.")


def plan(*, brief: str | None = None, from_reference: bool = False, only: list[str] | None = None,
         have_canonical: bool = False) -> list[Turn]:
    """Which turns will run. `only` limits the expressions; with an existing canonical response
    (from a previous run) those can chain from it without re-rendering idle."""
    unknown = [e for e in only or [] if e not in EXPRESSIONS]
    if unknown:
        raise AvatarError(f"unknown expression(s) {unknown}; choose from {list(EXPRESSIONS)}")
    wanted = only or list(EXPRESSIONS)
    turns = []
    if "idle" in wanted or not have_canonical:
        turns.append(Turn("idle", canonical_prompt(brief, from_reference), chained=False, reference=from_reference))
    turns += [Turn(e, expression_prompt(e), chained=True) for e in wanted if e != "idle"]
    return turns


def _image(response: Any) -> bytes:
    for item in getattr(response, "output", None) or []:
        kind = item.get("type") if isinstance(item, dict) else getattr(item, "type", None)
        if kind == "image_generation_call":
            result = item.get("result") if isinstance(item, dict) else getattr(item, "result", None)
            if result:
                return base64.b64decode(result)
    raise AvatarError(f"Muse Image returned no image (status {getattr(response, 'status', '?')})")


def write_manifest(out_dir: Path, entries: dict, meta: dict) -> None:
    """manifest.json for people, manifest.js for the desktop renderer (file:// can't fetch JSON)."""
    data = {**meta, "expressions": entries}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "manifest.json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "manifest.js").write_text(
        "// Generated by scripts/make_avatar.py / scripts/import_avatar.py. Lantern's face, made with Muse.\n"
        f"window.LanternAvatarManifest = {json.dumps(data, ensure_ascii=False)};\n", encoding="utf-8")


def read_manifest(out_dir: Path) -> dict:
    path = out_dir / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"expressions": {}}


def generate(client: Any, out_dir: str | Path, *, brief: str | None = None, reference: tuple[bytes, str] | None = None,
             only: list[str] | None = None, on_cost: Callable[[float], None] | None = None,
             on_progress: Callable[[str], None] | None = None) -> Sheet:
    """Render the expression sheet with Muse Image. `reference` = (image bytes, file suffix) of your own art."""
    out = Path(out_dir)
    manifest = read_manifest(out)
    canonical_id = manifest.get("canonical_response", "") if only and "idle" not in only else ""
    turns = plan(brief=brief, from_reference=reference is not None, only=only, have_canonical=bool(canonical_id))
    sheet = Sheet(out, canonical_response=canonical_id)
    entries = dict(manifest.get("expressions", {}))
    for turn in turns:
        tools = [{"type": "image_generation", "size": SIZE}]
        if turn.reference:
            data, suffix = reference
            mime = MIME.get(suffix.lower())
            if not mime:
                raise AvatarError(f"use a PNG, JPEG or WebP image, not {suffix}")
            url = f"data:{mime};base64," + base64.b64encode(data).decode()
            response = client.responses.create(model=models.IMAGE, tools=tools, input=[{"role": "user", "content": [
                {"type": "input_text", "text": turn.prompt}, {"type": "input_image", "image_url": url}]}])
        elif turn.chained:
            if not sheet.canonical_response:
                raise AvatarError("no canonical portrait to anchor on; run without --only first")
            response = client.responses.create(model=models.IMAGE, tools=tools, input=turn.prompt,
                                               previous_response_id=sheet.canonical_response)
        else:
            response = client.responses.create(model=models.IMAGE, tools=tools, input=turn.prompt)
        image = _image(response)
        if not turn.chained:
            sheet.canonical_response = response.id
        (out / f"{turn.expression}.webp").parent.mkdir(parents=True, exist_ok=True)
        (out / f"{turn.expression}.webp").write_bytes(image)
        cost = models.image_cost(1)
        sheet.cost_usd += cost
        if on_cost:
            on_cost(cost)
        entries[turn.expression] = {"file": f"{turn.expression}.webp", "source": "Muse Image (muse-image-1.0)",
                                    "response_id": response.id, "prompt": turn.prompt}
        sheet.made[turn.expression] = out / f"{turn.expression}.webp"
        if on_progress:
            on_progress(turn.expression)
        write_manifest(out, entries, {"made_with": "Muse Image", "model": models.IMAGE,
                                      "canonical_response": sheet.canonical_response, "updated_at": time.time()})
    return sheet
