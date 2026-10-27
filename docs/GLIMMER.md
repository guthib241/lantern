# The private lane: Muse Glimmer on your laptop

Lantern reads files in **private** zones only with Muse Glimmer running on your own machine. The cloud model gets Glimmer's short answer, scrubbed, and never the file. Without Glimmer, Lantern **refuses** private-file questions: there is no cloud fallback and no stand-in outside rehearsal.

Your laptop (from `docs/DECISIONS.md`) has an RTX 4060 with 8 GB VRAM and 15.6 GB RAM. The official Q4_K_M build (~19 GB) doesn't fit, so use a 2-bit build (IQ2_M or IQ2_XXS, about 9–11 GB), text only (skip the vision projector), with part of the model on the GPU.

## 1. Get llama.cpp and the model

- llama.cpp: download a CUDA build of `llama-server` from the llama.cpp releases page, or build it with `-DGGML_CUDA=ON`.
- The model: take the Glimmer GGUF that dev.meta.ai's Muse Glimmer page lists for local use, in the smallest 2-bit quantization it offers. Save it as `C:\models\glimmer-IQ2_M.gguf` (or `~/models/…`). The vision projector (`mmproj`) is not needed.

## 2. Start it (keep this window open while you use Lantern)

```bash
llama-server -m ~/models/glimmer-IQ2_M.gguf --host 127.0.0.1 --port 8080 -c 8192 -ngl 20 --jinja
```

- `-ngl 20` puts 20 layers on the GPU. Raise it until VRAM is nearly full (Task Manager → GPU), and lower it if it crashes with "out of memory".
- `-c 8192` is enough for questions about one file. Lantern sends at most a file's first part plus the question.
- It listens on 127.0.0.1 only, so nothing on your network can reach it.

## 3. Point Lantern at it

In `.env`:

```bash
LANTERN_GLIMMER_URL=http://127.0.0.1:8080/v1
```

Check:

```bash
lantern --doctor                      # "Private lane (Muse Glimmer)  ✓ … answers"
python scripts/smoke.py glimmer       # one real local question
lantern --set-zone ~/Private private  # make sure the demo's folder is private
```

The panel's step badge reads **Muse Glimmer (local)**, and the Privacy tab counts the question under "stayed on this computer".

## If it's too slow or doesn't fit

- Close Chrome and other heavy apps during the demo; Electron, Lantern's own browser and the model share 15.6 GB of RAM.
- Try IQ2_XXS (smaller), or `-c 4096`.
- Backup: run Glimmer on a GPU host you control (Modal, Baseten). Set `LANTERN_GLIMMER_URL` to its OpenAI-compatible URL, plus `LANTERN_GLIMMER_KEY` if it needs one. Lantern audits it as **self-hosted**, shown separately from local.
