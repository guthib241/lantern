"""Settings for Lantern's self-hosted Muse Glimmer server on Modal.

Pure standard library (no `modal` import), so tests can check it without the
Modal client. glimmer_app.py, smoke.py and rotate_token.py all read from here.

Sources (read Oct 28, 2026):
  - dev.meta.ai/docs/muse-glimmer/deploy: served name "muse-glimmer", sampling
    temperature 1.0 / top_p 0.95 / top_k 64 (never 0), reasoning_strength via
    chat_template_kwargs, llama.cpp build b10353 or newer.
  - huggingface.co/meta-models/Muse-Glimmer-30B-GGUF model card: file sizes,
    sha256, 52 layers (13 global, 39 sliding-window 2048), 2 KV heads x 128.
  - `modal billing rates`: GPU / CPU / memory prices.

Deploy-time knobs (environment variables when running `modal deploy`):
  LANTERN_GLIMMER_APP    Modal app name, default lantern-glimmer
  LANTERN_GLIMMER_SECRET Modal Secret holding the token, default lantern-private
                         (token file ~/.lantern/<secret>_token when not the default)
  LANTERN_GLIMMER_GPU    A10 (default) | L4 | L40S | ...   see GPU_PRICE_PER_HOUR
                         A10 is the default because it was measured (22 s cold start, 29.6 tokens/s,
                         claude/real-run, Oct 30); L4 had no capacity (10+ min unscheduled) that day
                         CPU = no GPU: llama.cpp's CPU build on CPU_CORES cores (slow, but runs on Modal
                         credit alone; Modal needs a card on file before any GPU function deploys)
  LANTERN_GLIMMER_QUANT  q4_k_m (default) | q4_k_xl
  LANTERN_GLIMMER_DRAFT  1 (default) = DFlash drafter for speculative decoding, 0 = off
  LANTERN_GLIMMER_CTX    context tokens for the single slot, default 16384
  LANTERN_GLIMMER_IDLE   seconds an idle GPU container stays up, default 300
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

TOKEN_KEY = "LANTERN_PRIVATE_TOKEN"
VOLUME_NAME = "lantern-glimmer-weights"   # shared by every deployment; the server mounts it read-only
MODEL_DIR = "/models"
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def names_from_env(env: dict) -> tuple[str, str, str]:
    """(app name, Secret name, token file). LANTERN_GLIMMER_APP / LANTERN_GLIMMER_SECRET give a second,
    independent deployment its own app and token, so two people or sessions on one Modal workspace
    can't stop each other's app or rotate each other's token."""
    app = env.get("LANTERN_GLIMMER_APP") or "lantern-glimmer"
    secret = env.get("LANTERN_GLIMMER_SECRET") or "lantern-private"
    for value in (app, secret):
        if not _NAME_RE.fullmatch(value):
            raise ValueError(f"not a valid Modal name: {value!r}")
    token_file = "~/.lantern/private_token" if secret == "lantern-private" else f"~/.lantern/{secret}_token"
    return app, secret, token_file


APP_NAME, SECRET_NAME, DEFAULT_TOKEN_FILE = names_from_env(os.environ)

# Official llama.cpp server image, CUDA 12 build, pinned. Glimmer needs b10353+.
LLAMA_IMAGE = "ghcr.io/ggml-org/llama.cpp:server-cuda-b11913"
LLAMA_IMAGE_CPU = "ghcr.io/ggml-org/llama.cpp:server-b11913"   # same build, CPU only
LLAMA_SERVER = "/app/llama-server"
SERVED_MODEL = "muse-glimmer"
LLAMA_PORT = 8081  # inside the GPU container, bound to 127.0.0.1 only

HF_REPO = "meta-models/Muse-Glimmer-30B-GGUF"
HF_REVISION = "b44f1072cf75e918a1b82a3a17b45c4389b076e7"  # pinned commit


@dataclass(frozen=True)
class WeightFile:
    filename: str
    size_bytes: int
    sha256: str


WEIGHTS: dict[str, WeightFile] = {
    "q4_k_m": WeightFile("Muse-Glimmer-30B-KQuant-17GB-Q4_K_M.gguf", 16_756_683_904,
                         "4cc57c0f51040a226e5a72cc47b7613f7772950e460a665f7083de89f183f60e"),
    "q4_k_xl": WeightFile("Muse-Glimmer-30B-KQuant-Dynamic-Q4_K_XL.gguf", 19_653_960_832,
                          "ac7023d6a4c704eb9af54ab53e476a66b7f5b6c0ef2fc4a8dde5253c291a6c38"),
}
DRAFT = WeightFile("dflash-Muse-Glimmer-30B-Q4_K_M.gguf", 1_631_208_128,
                   "b2e808bf656086fe86bd0d0bd990f01d33e377537a07c02d45371517c8b264ef")

# USD per hour, from `modal billing rates` on Oct 28, 2026. Modal GPU names as the client accepts them.
GPU_PRICE_PER_HOUR = {"CPU": 0.0, "T4": 0.59, "L4": 0.80, "A10": 1.10, "L40S": 1.95, "A100-40GB": 2.10,
                      "A100-80GB": 2.50, "RTX-PRO-6000": 3.03, "H100": 3.95}
GPU_VRAM_GIB = {"CPU": 30.0, "T4": 15.0, "L4": 22.0, "A10": 22.0, "L40S": 44.5, "A100-40GB": 39.5,
                "A100-80GB": 79.0, "RTX-PRO-6000": 94.5, "H100": 79.0}   # usable, not marketing GB
CPU_PER_CORE_HOUR = 0.0473
MEM_PER_GIB_HOUR = 0.008

# GPU container resources (requests; billing is the larger of request and use).
GPU_CPU_CORES = 1.0
GPU_MEMORY_MIB = 4096   # weights are read (no mmap) straight into VRAM, so host RAM stays small
# CPU mode: the weights live in RAM, and decoding speed follows cores and memory bandwidth.
CPU_CORES = 16.0
CPU_MEMORY_MIB = 32768

# Glimmer text model geometry (model card): KV cache per token.
_LAYERS_GLOBAL, _LAYERS_LOCAL, _WINDOW = 13, 39, 2048
_KV_BYTES_PER_TOKEN_PER_LAYER = 2 * 2 * 128 * 2   # K and V, 2 KV heads, head dim 128, f16
COMPUTE_OVERHEAD_GIB = 2.0   # CUDA context, compute buffers, 202k-vocab logits; generous on purpose

REASONING_STRENGTHS = ("low", "medium", "high", "xhigh")
SAMPLING = {"temperature": 1.0, "top_p": 0.95, "top_k": 64}
MAX_TOKENS_CAP = 8192
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,256}$")


class ConfigError(ValueError):
    """A deploy setting that can't work (unknown GPU, model too big for it, bad numbers)."""


def kv_cache_gib(context: int) -> float:
    """KV cache for one slot of `context` tokens."""
    local_tokens = min(context, _WINDOW)
    per_layer = _KV_BYTES_PER_TOKEN_PER_LAYER
    total = _LAYERS_GLOBAL * context * per_layer + _LAYERS_LOCAL * local_tokens * per_layer
    return total / 2**30


def vram_needed_gib(quant: str, context: int, draft: bool) -> float:
    w = WEIGHTS[quant].size_bytes + (DRAFT.size_bytes if draft else 0)
    return w / 2**30 + kv_cache_gib(context) + COMPUTE_OVERHEAD_GIB


@dataclass(frozen=True)
class ServeConfig:
    gpu: str = "A10"
    quant: str = "q4_k_m"
    draft: bool = True
    context: int = 16384
    idle_seconds: int = 300
    reasoning_strength: str = "low"

    def __post_init__(self) -> None:
        if self.gpu not in GPU_PRICE_PER_HOUR:
            raise ConfigError(f"unknown GPU {self.gpu!r}; pick one of {sorted(GPU_PRICE_PER_HOUR)}")
        if self.quant not in WEIGHTS:
            raise ConfigError(f"unknown quant {self.quant!r}; pick one of {sorted(WEIGHTS)}")
        if not 2048 <= self.context <= 131072:
            raise ConfigError("context must be between 2048 and 131072 tokens")
        if not 60 <= self.idle_seconds <= 1800:
            raise ConfigError("idle timeout must be 60..1800 seconds (scale to zero quickly)")
        if self.gpu == "CPU" and self.draft:
            raise ConfigError("the drafter needs a GPU; use LANTERN_GLIMMER_DRAFT=0 with LANTERN_GLIMMER_GPU=CPU")
        if self.reasoning_strength not in REASONING_STRENGTHS:
            raise ConfigError(f"reasoning_strength must be one of {REASONING_STRENGTHS}")
        need = vram_needed_gib(self.quant, self.context, self.draft)
        if need > GPU_VRAM_GIB[self.gpu]:
            raise ConfigError(f"{self.quant} (+draft={self.draft}) at {self.context} tokens needs ~{need:.1f} GiB; "
                              f"{self.gpu} has {GPU_VRAM_GIB[self.gpu]} GiB")

    @classmethod
    def from_env(cls, env: dict) -> "ServeConfig":
        def num(key: str, default: int) -> int:
            raw = env.get(key, "")
            try:
                return int(raw) if raw else default
            except ValueError:
                raise ConfigError(f"{key} must be a whole number, got {raw!r}") from None
        return cls(
            gpu=env.get("LANTERN_GLIMMER_GPU") or cls.gpu,
            quant=env.get("LANTERN_GLIMMER_QUANT") or cls.quant,
            draft=(env.get("LANTERN_GLIMMER_DRAFT") or ("0" if env.get("LANTERN_GLIMMER_GPU") == "CPU" else "1"))
            not in ("0", "false", "no", "off"),
            context=num("LANTERN_GLIMMER_CTX", cls.context),
            idle_seconds=num("LANTERN_GLIMMER_IDLE", cls.idle_seconds),
            reasoning_strength=env.get("LANTERN_GLIMMER_REASONING") or cls.reasoning_strength,
        )

    def as_env(self) -> dict[str, str]:
        """The same settings as env vars, so the container rebuilds an identical config."""
        return {"LANTERN_GLIMMER_GPU": self.gpu, "LANTERN_GLIMMER_QUANT": self.quant,
                "LANTERN_GLIMMER_DRAFT": "1" if self.draft else "0",
                "LANTERN_GLIMMER_CTX": str(self.context), "LANTERN_GLIMMER_IDLE": str(self.idle_seconds),
                "LANTERN_GLIMMER_REASONING": self.reasoning_strength}

    def files(self) -> list[WeightFile]:
        return [WEIGHTS[self.quant]] + ([DRAFT] if self.draft else [])

    @property
    def cpu_only(self) -> bool:
        return self.gpu == "CPU"

    def resources(self) -> tuple[float, int]:
        """(CPU cores, memory MiB) the server container requests."""
        return (CPU_CORES, CPU_MEMORY_MIB) if self.cpu_only else (GPU_CPU_CORES, GPU_MEMORY_MIB)

    def price_per_hour(self) -> float:
        if self.cpu_only:
            return CPU_CORES * CPU_PER_CORE_HOUR + CPU_MEMORY_MIB / 1024 * MEM_PER_GIB_HOUR
        return (GPU_PRICE_PER_HOUR[self.gpu] + GPU_CPU_CORES * CPU_PER_CORE_HOUR
                + GPU_MEMORY_MIB / 1024 * MEM_PER_GIB_HOUR)


def llama_server_cmd(cfg: ServeConfig, model_dir: str = MODEL_DIR) -> list[str]:
    """The llama-server command line. Listens on 127.0.0.1 only; the gate is the only way in."""
    cmd = [
        LLAMA_SERVER,
        "-m", f"{model_dir}/{WEIGHTS[cfg.quant].filename}",
        "-a", SERVED_MODEL,
        "--host", "127.0.0.1", "--port", str(LLAMA_PORT),
        "-ngl", "0" if cfg.cpu_only else "999",   # every layer on the GPU (or none, in CPU mode)
        "-c", str(cfg.context), "-np", "1",   # one slot gets the whole context (-c is split across slots)
        "--jinja",                      # required: the chat template (and tool-call format) is in the GGUF
        "--load-mode", "none",          # no mmap: read weights into VRAM/RAM once (b11913 dropped --no-mmap; live, Oct 28)
        "--temp", str(SAMPLING["temperature"]), "--top-p", str(SAMPLING["top_p"]),
        "--top-k", str(SAMPLING["top_k"]), "--min-p", "0",   # Glimmer's own generation config, nothing extra
        "--chat-template-kwargs", '{"reasoning_strength":"%s"}' % cfg.reasoning_strength,
    ]
    if cfg.cpu_only:
        cmd += ["-t", str(int(CPU_CORES))]
    if cfg.draft:
        cmd += ["-md", f"{model_dir}/{DRAFT.filename}", "-ngld", "999"]
    return cmd


def validate_token(token: str | None) -> str:
    """The gate refuses to start without a strong token (token_urlsafe(32) is 43 chars)."""
    if not token or not TOKEN_RE.fullmatch(token):
        raise ConfigError(f"{TOKEN_KEY} is missing or too weak; run deploy/modal/rotate_token.py")
    return token


def cost_usd(seconds: float, cfg: ServeConfig) -> float:
    """Estimated cost of a GPU container that ran for `seconds`."""
    return cfg.price_per_hour() * seconds / 3600
