"""The one place where Meta model IDs, endpoints and prices live.

Prices are from https://dev.meta.ai/docs/pricing-rate-limits (read Oct 26, 2026),
in USD. Token prices are per 1M tokens.

Rule: never send user data to a `-contributor` model (Meta trains on
Contributor-mode content). `check_model` enforces that in code.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

BASE_URL = "https://api.meta.ai/v1"
API_KEY_ENV = "MODEL_API_KEY"

SPARK = "muse-spark-1.3"
WONDER = "muse-wonder-1.0"
IMAGE = "muse-image-1.0"
VOICE = "muse-voice-transcribe-1.0"
SAM = "sam-3.1"
SPARK_OPEN_BASETEN = "meta-models/muse-spark-open-1026"
GLIMMER_LOCAL = "muse-glimmer"  # whatever name the local OpenAI-compatible server reports

TRANSCRIBE_PATH = "/asr/transcribe"


@dataclass(frozen=True)
class TokenPrice:
    input: float
    cached_input: float
    output: float


TOKEN_PRICES: dict[str, TokenPrice] = {
    SPARK: TokenPrice(input=1.25, cached_input=0.15, output=4.25),
    WONDER: TokenPrice(input=4.00, cached_input=0.20, output=20.00),
    # Self-hosted models cost nothing per token here (hardware is paid elsewhere).
    GLIMMER_LOCAL: TokenPrice(0.0, 0.0, 0.0),
    SPARK_OPEN_BASETEN: TokenPrice(0.0, 0.0, 0.0),
}

PER_IMAGE = {IMAGE: 0.01}            # per generated image
PER_AUDIO_HOUR = {VOICE: 0.18}       # metered in whole seconds, rounded down
PER_1K_IMAGES = {SAM: 2.50}          # image segmentation

# What each model is for; shown as the "model badge" in the UI.
BADGES = {
    SPARK: "Muse Spark",
    WONDER: "Muse Wonder",
    IMAGE: "Muse Image",
    VOICE: "Muse Voice Transcribe",
    SAM: "SAM 3.1",
    GLIMMER_LOCAL: "Muse Glimmer (local)",
    SPARK_OPEN_BASETEN: "Muse Spark Open",
}


class ModelPolicyError(ValueError):
    """Raised when code tries to use a model that is not allowed on user data."""


def check_model(model: str) -> str:
    """Return `model` if it may see user data, else raise."""
    if model.endswith("-contributor") or "-contributor" in model:
        raise ModelPolicyError(
            f"{model} is a Contributor-mode model; Meta trains on that data, "
            "so Lantern never sends user content to it"
        )
    return model


def badge(model: str) -> str:
    return BADGES.get(model, model)


def _get(obj: Any, name: str, default: Any = 0) -> Any:
    """Read a field from an SDK object or a plain dict."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0

    @classmethod
    def from_response(cls, usage: Any) -> "Usage":
        """Parse a Responses API `usage` object (SDK object or dict). Missing = zeros."""
        if usage is None:
            return cls()
        return cls(
            input_tokens=int(_get(usage, "input_tokens", 0) or 0),
            cached_tokens=int(_get(_get(usage, "input_tokens_details", None), "cached_tokens", 0) or 0),
            output_tokens=int(_get(usage, "output_tokens", 0) or 0),
            reasoning_tokens=int(_get(_get(usage, "output_tokens_details", None), "reasoning_tokens", 0) or 0),
        )


def token_cost(model: str, usage: Usage) -> float:
    """USD cost of one call. Cached tokens are part of input_tokens, billed at the cached rate.
    Reasoning tokens are already counted inside output_tokens."""
    price = TOKEN_PRICES.get(model)
    if price is None:
        return 0.0
    cached = min(usage.cached_tokens, usage.input_tokens)
    fresh = usage.input_tokens - cached
    return (fresh * price.input + cached * price.cached_input + usage.output_tokens * price.output) / 1_000_000


def image_cost(n_images: int) -> float:
    return n_images * PER_IMAGE[IMAGE]


def audio_cost(duration_ms: int) -> float:
    return (duration_ms // 1000) / 3600 * PER_AUDIO_HOUR[VOICE]


def sam_cost(n_images: int) -> float:
    return n_images * PER_1K_IMAGES[SAM] / 1000
