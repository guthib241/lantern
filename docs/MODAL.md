# The private lane on Modal: a self-hosted Muse Glimmer server

Lantern's private lane is any OpenAI-compatible Muse Glimmer server (see `docs/GLIMMER.md` for the laptop version). This page covers running it on a rented GPU on **Modal**: a **self-hosted private Glimmer server** that only Lantern can reach, with its own access token.

**Wording for judges and the README:** say *"self-hosted private Glimmer server"* (or "Muse Glimmer on our own private GPU server"). Do **not** say "local", "on-device" or "never leaves your computer" for this setup. Private-file questions travel over TLS to a GPU we rent from Modal, are answered there, and nothing is logged or stored, but they do leave the laptop. Lantern already audits this lane as `selfhosted`, separately from `local`.

## Status (Oct 28, 2026)

| Part | State |
|---|---|
| Weights in the Modal Volume `lantern-glimmer-weights` | **Done.** Q4_K_M (16.8 GB) + DFlash drafter (1.6 GB), downloaded once (93 s + 12 s) and sha256-checked |
| Modal Secret `lantern-private` + `~/.lantern/private_token` | **Done** (`rotate_token.py`) |
| Token gate (`gate.py`) | **Done, tested live** on Modal with the real Secret and HTTPS URL (fake GPU behind it) |
| GPU server (`Glimmer` class) | **Blocked:** Modal answered `Please add a payment method to use L4 GPU functions.` The account has credit but no card on file. Add one at modal.com → Settings → Usage & Billing, then run the smoke test below |
| Cold start, tokens/sec, tool calling on the real model | **Not measured yet.** Waiting on the payment method. The figures below marked *estimate* are estimates |

## GPU, measured (Oct 30, card on file)

- **L4:** no capacity. Modal kept the function "waiting to be scheduled on a GPU_L4 worker" for over 10 minutes, and the gate answered 500 meanwhile.
- **A10 (`LANTERN_GLIMMER_GPU=A10`)**, Q4_K_M + DFlash drafter, `smoke.py`: cold start **22 s**, decode **29.6 tokens/s** (prompt 367 tokens/s; 171 of 579 drafted tokens accepted), short answer right, one tool call with the right arguments and a correct follow-up. A10 is now the recommended GPU: it scheduled at once and is ~7× faster than CPU mode.
- Lantern's private-file check (`scripts/real_run.py --only 2`): **44 s** end to end (118 s on CPU), nothing private in any Meta request.
- **One workspace, one app name, one Secret:** two sessions deploying `lantern-glimmer` or running `rotate_token.py` at the same time overwrite each other (the other one's token stops working). Use one session (or the laptop) for the Modal app at a time.

## CPU mode: running before a card is on file (Oct 28, measured)

Modal refuses every GPU function (L4, A10 and L40S were tried) until a payment method is on the account, even with credit. CPU functions run on credit, so the same app has a CPU mode:

```bash
LANTERN_GLIMMER_GPU=CPU modal deploy deploy/modal/glimmer_app.py
```

- llama.cpp's CPU build (`server-b11913`), 16 cores, 32 GiB, the same Q4_K_M weights from the Volume, no drafter. About **$1.01/h** while warm, $0 when scaled to zero.
- **Measured:** cold start 149 s (17 GB into RAM), then **4.2 tokens/s**. A private question with a short answer took about 1–2 min warm. That's fine for a proof and for a private question now and then, too slow for chat.
- Same gate, token, scale-to-zero and logging rules as the GPU path. Lantern points at it the same way (below), with `LANTERN_GLIMMER_KEY_FILE=~/.lantern/private_token`.
- **Also fixed on the way:** llama.cpp b11913 no longer knows `--no-mmap` (the server crash-looped with "invalid argument"); it is `--load-mode none` now, on the GPU path too.

## How it fits together

```
Lantern router ──HTTPS, Bearer token──▶ gate (CPU, 0.25 core)        ──Modal function call──▶ Glimmer (L4 GPU)
 (OpenAI SDK)                           the only public URL                                   llama-server on 127.0.0.1
                                        401 / 404 / 413 / 400 here                            Q4_K_M + DFlash drafter
                                        never wakes the GPU                                   weights from the Volume
```

- **gate** (`deploy/modal/gate.py`, run by `glimmer_app.py::gate`) is the only web endpoint. Every request needs `Authorization: Bearer <token>`, compared in constant time. Only `GET /v1/models` and `POST /v1/chat/completions` exist; everything else gets 404. It refuses to start if the Secret is missing or weak. Chat requests are normalized: Glimmer's sampling (temperature 1.0, top_p 0.95, top_k 64) fills in missing values, `temperature <= 0` is raised to 1.0 (greedy decoding makes Glimmer loop), `parallel_tool_calls: false` (one tool call per turn), `max_tokens` is capped at 8192, and streaming is refused.
- **Glimmer** (GPU class) has **no web endpoint**. Only the gate can call it, over a Modal function call that needs the workspace's credentials. llama-server listens on 127.0.0.1 inside that container. A request without the token therefore costs a fraction of a cent of CPU and never starts a GPU. That matters because `*.modal.run` URLs are predictable from the workspace and app names.
- **No content logging.** The gate never prints. llama-server runs at its default verbosity (timings and token counts, no text), and the token is not on its command line. The smoke test sends a random canary word and fails if it appears in `modal app logs`. The live gate probe's logs contained neither the token nor any message text.
- **Scale to zero.** The GPU container stops after 5 minutes idle (`LANTERN_GLIMMER_IDLE`), the gate after 60 s. `max_containers=1` on the GPU, so the worst case is one GPU, never a fleet.

## Why llama.cpp on an L4

**Engine: llama.cpp `server-cuda-b11913`** (official image, pinned; Glimmer needs b10353+). dev.meta.ai's deploy page lists no public FP8 build of Glimmer. The NVFP4 build needs a Blackwell GPU (B200, $6.25/h). The quantized builds that fit a 24 GB card are the official GGUFs, and the GGUF model card documents llama.cpp for them, with tool calls parsed from the embedded template (`--jinja`). The vLLM flags from the earlier session (`--tool-call-parser muse_glimmer --reasoning-parser muse_glimmer --generation-config auto`) are for the BF16 checkpoint, which needs ~72 GB of VRAM. They are kept below as the "full precision" option.

**GPU: NVIDIA L4 (24 GB), $0.80/h.** It is the cheapest Modal GPU the model fits on:

| GPU (usable VRAM) | $/h | Q4_K_M + drafter at 16K ctx (≈19.4 GiB) | |
|---|---|---|---|
| T4 (15 GiB) | 0.59 | doesn't fit (the 16.8 GB file alone) | |
| **L4 (22 GiB)** | **0.80** | **fits, ~2.6 GiB spare** | **chosen** |
| A10 (22 GiB) | 1.10 | fits | ~2× the memory bandwidth, so roughly 2× the decode speed (estimate) |
| L40S (44.5 GiB) | 1.95 | fits, also fits Q4_K_XL + drafter | |
| A100-80GB / H100 | 2.50 / 3.95 | BF16 full precision with vLLM | |

VRAM math (`glimmer_config.vram_needed_gib`, from the model card): 15.6 GiB weights + 1.5 GiB drafter + 0.28 GiB KV cache + 2 GiB overhead. The KV cache stays tiny because only 13 of the 52 layers attend globally (the other 39 use a 2048-token window) and there are only 2 KV heads. 32K context would add only ~0.2 GiB.

**Speed trade-off (estimate, to be measured):** decoding is memory-bandwidth-bound. The L4 (300 GB/s) reading ~17 GB per token caps near 15–18 tok/s without speculation. The DFlash drafter should help: Meta reports 3.1× on an RTX 5090 with greedy decoding, and less is likely at temperature 1.0. If the smoke test shows L4 too slow for the demo, `--gpu A10` costs 1.4× per hour and should be ~2× faster, which makes it cheaper per token.

**Quality:** Q4_K_M ("K-Quant-17GB") is what the model card targets for 24 GB, with 1.0% average degradation versus full precision across 15 benchmarks. Q4_K_XL (0.2%) fits an L4 only without the drafter (`--quant q4_k_xl --no-draft`), or an L40S with it. Full precision means BF16 with vLLM on an 80 GB GPU (3–5× the price per hour). Not built here.

## Deploy, test, stop

One-time setup (laptop or any machine):

```bash
pip install modal openai          # openai only for smoke.py
modal token new                   # or set MODAL_TOKEN_ID / MODAL_TOKEN_SECRET
python deploy/modal/rotate_token.py   # makes the token: Modal Secret + ~/.lantern/private_token
```

Deploy, smoke-test, stop (one command; the weights are already in the Volume, so no download):

```bash
python deploy/modal/smoke.py                      # L4 + drafter (defaults); prints a JSON report, then stops the app
python deploy/modal/smoke.py --no-draft           # compare speed without the drafter
python deploy/modal/smoke.py --gpu A10            # compare a faster GPU
```

The smoke test deploys, then checks that the gate refuses no token, a wrong token, Basic auth and an unknown path (all before any GPU starts). It then measures cold start (the first authorized request), sends one short prompt with the router's exact parameters, makes one tool call and sends the tool result back for the follow-up answer, times a ~200-word answer for tokens/sec (llama-server's `timings`, including drafter acceptance), checks the logs for the canary, runs `modal app stop`, and estimates cost from GPU seconds × the hourly price. Target spend: under $0.15 per run.

For the demo, deploy and leave it deployed (it scales to zero):

```bash
modal deploy deploy/modal/glimmer_app.py          # prints the gate URL: https://<workspace>--lantern-glimmer-gate.us-east.modal.run
modal app stop lantern-glimmer                    # everything off; deploy again to bring it back
```

Settings are environment variables at deploy time (`LANTERN_GLIMMER_GPU`, `LANTERN_GLIMMER_QUANT`, `LANTERN_GLIMMER_DRAFT`, `LANTERN_GLIMMER_CTX`, `LANTERN_GLIMMER_IDLE`, `LANTERN_GLIMMER_REASONING`). See `deploy/modal/glimmer_config.py`; impossible combinations (too big for the GPU, idle > 30 min) are refused before deploying.

## Cost

| | $/hour | When |
|---|---|---|
| GPU container (L4 + 1 core + 4 GiB) | **$0.879** | only while warm: from the first request until 5 min after the last |
| Gate (0.25 core + 0.25 GiB) | $0.014 | while warm, 60 s after the last request |
| Idle (deployed, scaled to zero) | **$0** | weights Volume 18 GB is inside Modal's 1 TiB free storage |

- One "wake" (cold start + a few questions + 5 min idle) costs about **$0.08–0.10** (estimate).
- An hour of demo rehearsal with it warm the whole time costs about $0.90. The $100 credit covers ~110 warm hours.
- Per-request cost depends on tokens/sec, which hasn't been measured yet.
- If the token ever leaks, the worst case is someone keeping one L4 warm: $21/day. Rotate the token (below), and consider a workspace spending limit in Modal's billing settings.

**Cold start** (*estimate*): gate ~3.5 s (measured), then the GPU container starts and llama-server reads ~18 GB from the Volume into VRAM. Expect tens of seconds to ~1.5 min. `smoke.py` prints the real number. Modal web requests longer than 150 s get a 303 redirect that the OpenAI SDK follows automatically, so a slow cold start waits instead of failing. Lantern's router uses a 120 s timeout per request, so see "Router" below for a warm-up.

## Rotating the token (on your laptop)

```bash
python deploy/modal/rotate_token.py --redeploy
```

It makes a new `secrets.token_urlsafe(32)`, writes it to a temporary owner-only file, updates the Modal Secret `lantern-private`, and only then replaces `~/.lantern/private_token`. If the Modal call fails, the old token stays valid and in place. It never prints the token. `--redeploy` restarts the containers so the old token stops working right away; without it, running containers keep the old token until they scale down. On Windows the file goes to `C:\Users\<you>\.lantern\private_token`. Use `--token-file` to put it elsewhere.

The sandbox copy used for testing lives at `~/.lantern/private_token`, outside the repo, so it can't be committed.

## Router: what to change at merge time

The router already supports this lane: a non-loopback `LANTERN_GLIMMER_URL` is the `selfhosted` lane, and `LANTERN_GLIMMER_KEY` is sent as the bearer token. **With no code change**, this works today:

```bash
# .env (gitignored)
LANTERN_GLIMMER_URL=https://<workspace>--lantern-glimmer-gate.us-east.modal.run/v1
LANTERN_GLIMMER_KEY=<contents of ~/.lantern/private_token>
```

The proposed tiny change reads the key from the token file instead, so rotating needs no `.env` edit. In `lantern/router.py` (add `from pathlib import Path`), `RouterConfig.from_env`:

```python
        key_file = env.get("LANTERN_GLIMMER_KEY_FILE", "")
        file_key = Path(key_file).expanduser().read_text().strip() if key_file and Path(key_file).expanduser().is_file() else ""
        ...
            api_key=env.get("LANTERN_GLIMMER_KEY") or file_key or "local",
```

and in `.env`: `LANTERN_GLIMMER_KEY_FILE=~/.lantern/private_token`. This is Lantern's own state file, read the same way `server.py` reads `~/.lantern/token`, not a user file, so it doesn't need policy.py.

Three small follow-ups worth doing at the same time (not done here, since this branch only adds files):

1. **Honest system prompt.** `PRIVATE_SYSTEM` in `router.py` says "You run on the user's own computer". On the selfhosted lane, say "You run on the user's private server" instead.
2. **Cold-start warm-up.** `OpenAIGlimmer` uses a 120 s timeout. For the selfhosted lane, either raise it to 300 s, or have Lantern send one `GET /v1/models` in the background at startup so the GPU is warm before the first private question.
3. **Doctor.** `lantern --doctor` only probes localhost (by design, see DECISIONS). If wanted, a selfhosted check could call `GET /v1/models`, but that wakes the GPU (~$0.08), so keep it behind an explicit flag.

Also at merge time: add to `docs/THIRD_PARTY.md` llama.cpp (MIT, official `ghcr.io/ggml-org/llama.cpp` image), the Modal client (Apache-2.0), huggingface_hub (Apache-2.0) and the Muse Glimmer GGUF weights (Apache-2.0). Add a `BUILD_LOG.md` line, and a `docs/DECISIONS.md` line: "gate + RPC-only GPU so unauthenticated traffic never wakes a GPU; llama.cpp Q4_K_M on L4 because no public FP8 exists and BF16 needs 80 GB".

## Files

| File | What |
|---|---|
| `deploy/modal/glimmer_app.py` | The Modal app: weights build step, `Glimmer` GPU class, `gate` web endpoint |
| `deploy/modal/gate.py` | The token gate (pure ASGI, standard library) |
| `deploy/modal/glimmer_config.py` | All settings, prices, the llama-server command, VRAM and cost math |
| `deploy/modal/rotate_token.py` | New token → Modal Secret + `~/.lantern/private_token`, never printed |
| `deploy/modal/smoke.py` | Deploy, test, measure, stop |
| `tests/modal/` | Unit tests for config, gate and rotation (no Modal, no network) |

## Notes from building it in the cloud sandbox

- In Claude Code's cloud sandbox, `modal deploy`, `modal app stop`, `modal app logs`, Secrets, Volumes and HTTPS calls to `*.modal.run` all work. `modal run` / `.remote()` from the sandbox don't: Modal's function-call "input plane" (`input-plane.us-east.modal.com`, gRPC) is intercepted by the sandbox proxy without HTTP/2, and there's no `modal2.com` fallback for it (the control plane falls back to `api.modal2.com`, which is why everything else works). That's why the weights download is an image build step during `modal deploy`, and why every test goes through the HTTPS gate. On a laptop none of this matters.
- `SSL_CERT_FILE=/root/.ccr/ca-bundle.crt` is needed for the Modal CLI in the sandbox only.
