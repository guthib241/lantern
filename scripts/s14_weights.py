"""S14: copy Muse Glimmer's weights from Hugging Face into the Modal Volume the S14 runs read (no GPU, once).

    modal run scripts/s14_weights.py

Its own app, with no GPU function in it, so it runs on any Modal account (GPU functions need a payment method).
`meta-models/Muse-Glimmer-30B`: bf16 safetensors, ~60 GB, Apache 2.0, ungated (dev.meta.ai/docs/muse-glimmer).
"""
from __future__ import annotations

import time
from pathlib import Path

import modal

REPO = "meta-models/Muse-Glimmer-30B"
VOL = "/vol"
MODEL_DIR = f"{VOL}/Muse-Glimmer-30B"

app = modal.App("lantern-s14-weights")
volume = modal.Volume.from_name("lantern-glimmer", create_if_missing=True)
image = (modal.Image.debian_slim(python_version="3.12")
         .pip_install("huggingface_hub[hf_transfer]>=0.34")
         .env({"HF_HUB_ENABLE_HF_TRANSFER": "1"}))


@app.function(image=image, volumes={VOL: volume}, cpu=4, memory=8192, timeout=3 * 3600)
def download() -> dict:
    from huggingface_hub import snapshot_download
    t0 = time.time()
    snapshot_download(REPO, local_dir=MODEL_DIR)
    volume.commit()
    size = sum(p.stat().st_size for p in Path(MODEL_DIR).rglob("*") if p.is_file())
    return {"repo": REPO, "gb": round(size / 1e9, 1), "seconds": round(time.time() - t0)}


@app.local_entrypoint()
def main():
    print(download.remote())
