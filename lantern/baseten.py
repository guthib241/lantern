"""Muse Spark Open on Baseten: an optional second cloud lane. OFF by default.

Muse Spark via the Meta API stays the default for everything. This lane exists so the
open-weights Muse Spark can answer public (non-private) questions, text or images, when the
user turns it on. A missing key, or the lane being off, never changes any other path.

Wire format (read Oct 30, 2026; nothing here is guessed):
  - docs.baseten.co/inference/model-apis/overview: OpenAI-compatible Chat Completions at
    https://inference.baseten.co/v1, `Authorization: Bearer <key>` (the OpenAI SDK sends that).
  - docs.baseten.co/inference/model-apis/reasoning: Muse Spark Open always reasons. The
    reasoning comes back in `message.reasoning_content`, the answer in `message.content`;
    reasoning tokens are inside `usage.completion_tokens` and billed as output.
    `reasoning_effort`: low | medium | high | xhigh (the default; none/minimal run as low).
  - docs.baseten.co/inference/model-apis/vision: `image_url` content parts with `detail`;
    Muse Spark Open takes at most 8 images of 10 MB each, JPEG/PNG/WebP/GIF/BMP/TIFF.
  - baseten.co/pricing: $1.25 input / $0.15 cached input / $4.25 output per 1M tokens.
  - meta-model-cookbook 01_api_fundamentals/06_reasoning_tokens: `reasoning_content` is read
    from `message.model_extra`; `completion_tokens_details.reasoning_tokens` when present.

Rules kept in code:
  - Only models.SPARK_OPEN_BASETEN; any other model name is refused at config time.
  - The reasoning is never returned or shown: only `content` leaves this module.
  - Every request goes through GuardedClient on the *cloud* lane: scrubbed, the S0 deny list
    applies, audited. Images from files are read through policy.py, which refuses private
    zones: private content goes only to Muse Glimmer, never here.

Config (env or .env, never committed):
  LANTERN_BASETEN         off (default) | on
  BASETEN_API_KEY         the key. In the cloud sandbox: BASETEN_API_KEY=injected-by-proxy
                          (the environment's network secret adds the real one)
  LANTERN_BASETEN_EFFORT  low (default) | medium | high | xhigh
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import models
from .audit import AuditLog, EgressBlocked, GuardedClient

KEY_ENV = "BASETEN_API_KEY"
EFFORTS = ("low", "medium", "high", "xhigh")
MAX_IMAGES = 8
MAX_IMAGE_BYTES = 10 * 1024 * 1024
_IMAGE_MAGIC = [(b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", "image/png"), (b"GIF87a", "image/gif"),
                (b"GIF89a", "image/gif"), (b"BM", "image/bmp"), (b"II*\x00", "image/tiff"), (b"MM\x00*", "image/tiff")]
_ON = ("1", "on", "true", "yes")


class BasetenConfigError(ValueError):
    """A Baseten setting that can't be used (another model, an unknown effort)."""


class BasetenError(RuntimeError):
    """The Baseten lane can't answer (off, no key, refused, empty)."""


@dataclass(frozen=True)
class BasetenConfig:
    enabled: bool = False
    api_key: str = field(default="", repr=False)   # never printed
    model: str = models.SPARK_OPEN_BASETEN
    effort: str = "low"
    timeout: float = 120.0

    def __post_init__(self) -> None:
        if self.model != models.SPARK_OPEN_BASETEN:
            raise BasetenConfigError(f"Baseten is only used with {models.SPARK_OPEN_BASETEN}, not {self.model!r}")
        if self.effort not in EFFORTS:
            raise BasetenConfigError(f"LANTERN_BASETEN_EFFORT must be one of {EFFORTS}")

    @classmethod
    def from_env(cls, env: dict) -> "BasetenConfig":
        return cls(enabled=str(env.get("LANTERN_BASETEN", "off")).strip().lower() in _ON,
                   api_key=str(env.get(KEY_ENV, "")).strip(),
                   model=env.get("LANTERN_BASETEN_MODEL") or models.SPARK_OPEN_BASETEN,
                   effort=env.get("LANTERN_BASETEN_EFFORT") or cls.effort)

    @property
    def ready(self) -> bool:
        return self.enabled and bool(self.api_key)

    def why_not(self) -> str:
        if not self.enabled:
            return "the Baseten lane is off (LANTERN_BASETEN=on turns it on)"
        if not self.api_key:
            return f"LANTERN_BASETEN=on but {KEY_ENV} is not set"
        return ""


@dataclass(frozen=True)
class Reply:
    text: str                 # the final answer only; the reasoning is dropped
    usage: models.Usage
    cost_usd: float
    model: str = models.SPARK_OPEN_BASETEN


def image_type(data: bytes) -> str:
    """The image's media type from its first bytes, or "" if Muse Spark Open doesn't take it."""
    for magic, kind in _IMAGE_MAGIC:
        if data.startswith(magic):
            return kind
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def image_part(data: bytes) -> dict:
    kind = image_type(data)
    if not kind:
        raise BasetenError("not an image Muse Spark Open takes (JPEG, PNG, WebP, GIF, BMP or TIFF)")
    if len(data) > MAX_IMAGE_BYTES:
        raise BasetenError("image over 10 MB")
    url = f"data:{kind};base64,{base64.b64encode(data).decode('ascii')}"
    return {"type": "image_url", "image_url": {"url": url, "detail": "auto"}}


def read_image(policy: Any, path: str | Path) -> bytes:
    """An image file for this lane, read through policy.py: private zones are refused (they go to Glimmer only)."""
    return policy.read_bytes(path)   # allow_private stays False


def final_answer(message: Any) -> str:
    """`content` only. reasoning_content is never read. A server that inlines its thinking gets it cut off."""
    text = str(getattr(message, "content", None) or "")
    text = re.sub(r"(?s)<think>.*?</think>", "", text)
    return text.strip()


class SparkOpen:
    """Muse Spark Open on Baseten, behind the same guarded, audited client as the Meta API."""

    lane = "cloud"
    name = models.badge(models.SPARK_OPEN_BASETEN)

    def __init__(self, cfg: BasetenConfig, *, audit: AuditLog | None = None, memory: Any = None,
                 client: Any = None, deny: Callable[[], list[str]] | None = None):
        if not cfg.ready:
            raise BasetenError(cfg.why_not())
        self.cfg = cfg
        self.memory = memory
        if client is None:
            from openai import OpenAI
            client = OpenAI(base_url=models.BASETEN_BASE_URL, api_key=cfg.api_key, timeout=cfg.timeout,
                            max_retries=1)
        if audit is not None:
            client = GuardedClient(client, audit, lane="cloud", destination=models.BASETEN_BASE_URL)
            client.deny = deny
        self.client = client

    def ask(self, system: str, user: str, *, images: list[bytes] | None = None, task_id: int | None = None,
            max_tokens: int = 4096) -> Reply:
        """One question. `images`: raw bytes the caller read through policy.py (see read_image)."""
        images = list(images or [])
        if len(images) > MAX_IMAGES:
            raise BasetenError(f"at most {MAX_IMAGES} images per request")
        content: Any = user if not images else [{"type": "text", "text": user}, *map(image_part, images)]
        if isinstance(self.client, GuardedClient):
            self.client.set_task(task_id)
        try:
            r = self.client.chat.completions.create(
                model=models.check_model(self.cfg.model),
                messages=[{"role": "system", "content": system}, {"role": "user", "content": content}],
                max_tokens=max_tokens, reasoning_effort=self.cfg.effort)
        except EgressBlocked:   # private content: nothing was sent, and the user is told so
            raise
        except Exception as e:   # refused key, 5xx, timeout
            raise BasetenError(f"Muse Spark Open on Baseten didn't answer ({type(e).__name__})") from e
        usage = models.chat_usage(getattr(r, "usage", None))
        cost = models.token_cost(self.cfg.model, usage)   # reasoning tokens are inside completion_tokens
        if self.memory is not None:
            self.memory.log_usage(task_id, self.cfg.model, usage.input_tokens, usage.cached_tokens,
                                  usage.output_tokens, cost)
        choice = r.choices[0]
        text = final_answer(choice.message)
        if not text:
            why = "it used up max_tokens while reasoning" if choice.finish_reason == "length" else "an empty answer"
            raise BasetenError(f"Muse Spark Open gave no answer ({why})")
        return Reply(text, usage, cost, self.cfg.model)


def from_env(env: dict, **kw) -> SparkOpen | None:
    """The lane if it is on and has a key, else None. While it is off nothing else is even read."""
    if str(env.get("LANTERN_BASETEN", "off")).strip().lower() not in _ON:
        return None
    cfg = BasetenConfig.from_env(env)
    return SparkOpen(cfg, **kw) if cfg.ready else None
