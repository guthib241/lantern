"""Lantern's private lane on Modal: Muse Glimmer (Q4 GGUF, llama.cpp) behind a token gate.

    modal deploy deploy/modal/glimmer_app.py        # first run also downloads the weights (~18 GB, once)
    modal app stop lantern-glimmer                  # stop everything

Two parts:
  gate     CPU, the only web endpoint. Checks the bearer token from the Modal Secret
           `lantern-private` and forwards allowed requests (gate.py). Cheap to wake.
  Glimmer  GPU, no web endpoint. Runs llama-server on 127.0.0.1 inside its container,
           reached only by the gate's Modal function call. Scales to zero after
           LANTERN_GLIMMER_IDLE seconds (default 300).

Weights live in the Modal Volume `lantern-glimmer-weights`. They are downloaded by an
image build step (pinned revision, sha256-checked) that skips files already there, so
redeploys don't download again. Settings: see glimmer_config.py.

Nothing here logs request or response content: the gate never prints, and
llama-server runs at its default verbosity, which logs timings and token counts only
(checked by smoke.py, which looks for a canary word in the app logs).
"""
from __future__ import annotations

import os
import subprocess
import time
import urllib.error
import urllib.request

import modal

import glimmer_config as gc
from gate import ALLOWED, Gate

cfg = gc.ServeConfig.from_env(os.environ)

app = modal.App(gc.APP_NAME)
weights = modal.Volume.from_name(gc.VOLUME_NAME, create_if_missing=True)
private = modal.Secret.from_name(gc.SECRET_NAME, required_keys=[gc.TOKEN_KEY])


def download_weights(quant: str, draft: bool) -> None:
    """Image build step: put the GGUF files in the Volume once, verified against the pinned sha256."""
    import hashlib
    from pathlib import Path

    from huggingface_hub import hf_hub_download

    files = [gc.WEIGHTS[quant]] + ([gc.DRAFT] if draft else [])
    for f in files:
        path = Path(gc.MODEL_DIR) / f.filename
        marker = path.with_name(path.name + ".sha256-ok")
        if path.exists() and path.stat().st_size == f.size_bytes and marker.exists() \
                and marker.read_text().strip() == f.sha256:
            print(f"{f.filename}: already in the volume")
            continue
        t0 = time.monotonic()
        hf_hub_download(gc.HF_REPO, f.filename, revision=gc.HF_REVISION, local_dir=gc.MODEL_DIR)
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            while chunk := fh.read(64 << 20):
                h.update(chunk)
        if h.hexdigest() != f.sha256 or path.stat().st_size != f.size_bytes:
            path.unlink(missing_ok=True)
            raise RuntimeError(f"{f.filename}: checksum mismatch, deleted; deploy again to retry")
        marker.write_text(f.sha256)
        print(f"{f.filename}: downloaded and verified in {time.monotonic() - t0:.0f}s")
    weights.commit()


server_image = (
    modal.Image.from_registry(gc.LLAMA_IMAGE, add_python="3.12")
    .entrypoint([])
    .pip_install("huggingface_hub[hf_xet]==1.33.0")
    .env({"HF_HUB_DISABLE_TELEMETRY": "1", "HF_XET_HIGH_PERFORMANCE": "1"})
    .run_commands(f"{gc.LLAMA_SERVER} --version")
    .add_local_python_source("glimmer_config", "gate", copy=True)
    .run_function(download_weights, volumes={gc.MODEL_DIR: weights},
                  kwargs={"quant": cfg.quant, "draft": cfg.draft}, timeout=3600)
)
gate_image = modal.Image.debian_slim(python_version="3.12").add_local_python_source("glimmer_config", "gate")


@app.cls(image=server_image, gpu=cfg.gpu, cpu=gc.GPU_CPU_CORES, memory=gc.GPU_MEMORY_MIB,
         volumes={gc.MODEL_DIR: weights.read_only()}, env=cfg.as_env(),
         scaledown_window=cfg.idle_seconds, timeout=900, startup_timeout=600,
         min_containers=0, max_containers=1)
@modal.concurrent(max_inputs=4)   # llama-server has one slot; extra requests queue there, not on a new GPU
class Glimmer:
    @modal.enter()
    def start(self) -> None:
        t0 = time.monotonic()
        self.proc = subprocess.Popen(gc.llama_server_cmd(cfg), stdin=subprocess.DEVNULL)
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        deadline = t0 + 540
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"llama-server exited with code {self.proc.returncode}")
            try:
                with self._opener.open(f"http://127.0.0.1:{gc.LLAMA_PORT}/health", timeout=5) as r:
                    if r.status == 200:
                        break
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(1)
        else:
            raise RuntimeError("llama-server didn't become ready in 540 s")
        mem = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total", "--format=csv,noheader"],
                             capture_output=True, text=True).stdout.strip()
        print(f"lantern-glimmer ready in {time.monotonic() - t0:.1f}s on {mem} ({cfg})")

    @modal.method()
    def handle(self, method: str, path: str, body: bytes) -> tuple[int, bytes]:
        """Forward one request the gate already checked. Defense in depth: same allowlist here."""
        if (method, path) not in ALLOWED:
            return 404, b'{"error":{"message":"not found"}}'
        req = urllib.request.Request(f"http://127.0.0.1:{gc.LLAMA_PORT}{path}", method=method,
                                     data=body if method == "POST" else None,
                                     headers={"Content-Type": "application/json"})
        try:
            with self._opener.open(req, timeout=840) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    @modal.exit()
    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


@app.function(image=gate_image, secrets=[private], cpu=0.25, memory=256,
              scaledown_window=60, timeout=900, max_containers=2)
@modal.concurrent(max_inputs=16)
@modal.asgi_app()
def gate():
    glimmer = Glimmer()

    async def upstream(method: str, path: str, body: bytes) -> tuple[int, bytes]:
        return await glimmer.handle.remote.aio(method, path, body)

    return Gate(os.environ.get(gc.TOKEN_KEY), upstream)
