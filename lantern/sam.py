"""Camera check-in with SAM 3.1: "is my coffee mug here?" from one webcam snapshot, then a one-line reaction.

Strict scope, enforced in code:
  - only on an explicit click in the desktop app (the camera is allowed for one capture, then released),
  - one snapshot, kept in memory only, never written to disk,
  - objects only: concepts that name people, faces, bodies, emotions or moods are refused, so
    Lantern never analyses the user's face, emotions, health or personality.
Request (meta-model-cookbook 07_sam/01_api_basics): responses.create(model="sam-3.1", stream=True,
input=[{"type": "message", "role": "user", "content": [input_text <noun phrase>, input_image <data URL>]}]);
the streamed records are decoded with Meta's `meta_sam_parser`. One noun phrase per request.
Cost: $2.50 per 1,000 images, logged to memory.
"""
from __future__ import annotations

import asyncio
import random
import re
from typing import Any, Callable

from . import models

MAX_IMAGE_CHARS = 8_000_000      # ~6 MB of base64
IDEAS = ["coffee mug", "water glass", "plant", "laptop", "headphones", "book", "cat", "dog", "pizza", "notebook"]
DATA_URL = re.compile(r"^data:image/(jpeg|png|webp);base64,[A-Za-z0-9+/=]+$")
CONCEPT = re.compile(r"^[a-z][a-z' -]{1,40}$")
PEOPLE = re.compile(
    r"\b(face|faces|person|persons|people|man|men|woman|women|boy|girl|child|children|kid|kids|baby|human|"
    r"user|me|myself|eye|eyes|mouth|lips|nose|skin|hair|beard|body|head|hand|hands|arm|arms|leg|legs|"
    r"smile|frown|expression|emotion|emotions|mood|feeling|tired|sad|happy|angry|stress|sick|health|"
    r"tattoo|selfie|wrinkle|wrinkles|acne|weight)\b")
REACTIONS = {
    "coffee mug": "Ah, coffee. Good.",
    "water glass": "A water glass, spotted. I'll pretend I'm not impressed.",
    "plant": "The plant is still alive. One of you is doing well.",
    "cat": "A cat. Obviously the one in charge here.",
    "dog": "A dog! Professional opinion: excellent.",
    "pizza": "Pizza detected. I see how the evening is going.",
}


class CheckInError(ValueError):
    """The check-in can't run; the message is safe to show."""


def check_concept(text: str) -> str:
    concept = re.sub(r"\s+", " ", (text or "").strip().lower())
    if not CONCEPT.match(concept) or len(concept.split()) > 4:
        raise CheckInError("name one object in a few words, like 'coffee mug'")
    if PEOPLE.search(concept):
        raise CheckInError("Lantern only looks for objects, never people, faces or how anyone looks or feels")
    return concept


def reaction(concept: str, found: int) -> str:
    if not found:
        return random.choice([f"No {concept} in sight. Bold choice.", f"I looked. No {concept}. Suspicious."])
    if concept in REACTIONS:
        return REACTIONS[concept]
    return f"Found {found} {concept}{'' if found == 1 else 's'}. Carry on."


async def _as_events(stream: Any):
    """The SDK's typed stream events, as the plain dicts the parser reads."""
    for event in stream:
        yield event.model_dump() if hasattr(event, "model_dump") else dict(event)


def segment(client: Any, image_url: str, concept: str) -> dict:
    """One SAM 3.1 request; returns the objects found as boxes (pixels in the snapshot)."""
    from meta_sam_parser import ResponsesStreamError, image_segmentation_format, parse_responses_stream
    stream = client.responses.create(model=models.SAM, stream=True, input=[{
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": concept}, {"type": "input_image", "image_url": image_url}]}])

    async def parse():
        return await parse_responses_stream(_as_events(stream), image_segmentation_format()).final_result()
    try:
        result = asyncio.run(parse())
    except ResponsesStreamError as e:
        raise CheckInError(f"SAM couldn't finish: {str(e)[:160]}") from e
    if result.outcome.status != "completed":
        raise CheckInError(f"SAM stopped early ({getattr(result.outcome, 'reason', 'incomplete')})")
    boxes = [r for r in result.records if r.kind == "box"]
    return {"objects": len({b.object_id for b in boxes}),
            "boxes": [[b.left, b.top, b.right, b.bottom] for b in boxes],
            "warnings": [d.message for d in result.diagnostics if d.severity == "error"][:5]}


class CheckIn:
    def __init__(self, client: Any, memory=None, emit: Callable[[dict], None] | None = None):
        self.client = client
        self.memory = memory
        self.emit = emit or (lambda event: None)

    def run(self, image_url: str, concept: str) -> dict:
        concept = check_concept(concept)
        if len(image_url) > MAX_IMAGE_CHARS or not DATA_URL.match(image_url):
            raise CheckInError("send one JPEG, PNG or WebP snapshot as a data URL")
        found = segment(self.client, image_url, concept)
        cost = models.sam_cost(1)
        if self.memory is not None:
            self.memory.log_usage(None, models.SAM, cost_usd=cost)
        self.emit({"type": "usage", "model": models.SAM, "badge": models.badge(models.SAM), "cost_usd": cost})
        line = reaction(concept, found["objects"])
        self.emit({"type": "checkin", "concept": concept, "found": found["objects"], "reaction": line,
                   "badge": models.badge(models.SAM)})
        return {"concept": concept, "found": found["objects"], "boxes": found["boxes"], "reaction": line,
                "model": models.badge(models.SAM), "cost_usd": cost, "stored": False}
