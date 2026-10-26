# 🏮 Lantern

A private AI companion that lives on your computer, remembers what it's doing and what it has done, and gets real work done on your files. Built on Meta's Muse Spark for the Meta Global AI Developer Hackathon (Oct 26 – Nov 4, 2026).

## What works today (Day 1)

- **Agent loop on Muse Spark 1.3** (Responses API, tool calling, `previous_response_id` so reasoning carries across steps)
- **Memory** in a local SQLite file:
  - every task with status (`active` / `done` / `failed`) and a one-line summary of what it actually did
  - a step-by-step timeline of each task (plan, tool calls, results, reply)
  - lasting facts about you ("manager email", "tax folder"), learned during tasks
  - all of it is fed back to the model on every task, so Lantern knows what it already did
- **File tools**: list, search by name or content, read, write, move
- **Safety**: locked to folders you allow (no `../` or symlink escapes), asks before any change, never deletes, verifies every change on disk
- **Personalities**: `smug` (default), `sarcastic`, `helpful`

## Setup

```bash
pip install -r requirements.txt
export MODEL_API_KEY="your-key-from-dev.meta.ai"      # Windows: $env:MODEL_API_KEY="..."
export LANTERN_ROOTS="$HOME/Documents:$HOME/Downloads" # folders Lantern may touch (Windows: use ; between folders)
```

## Use

```bash
python -m lantern                                   # chat mode
python -m lantern "find my march invoice and move it to Documents/Tax"
python -m lantern --tasks                           # what Lantern has done
python -m lantern --task 3                          # full timeline of task 3
python -m lantern --memory                          # what it remembers about you
python -m lantern --persona sarcastic
```

## Tests

```bash
python -m pytest -q     # 16 tests, offline, using a scripted fake model
```

## Roadmap

| Days | Piece |
|---|---|
| Oct 26 | ✅ Agent loop, memory, safe file tools |
| Oct 27–29 | Phone control (WhatsApp), email sending, voice notes via Muse Voice Transcribe |
| Oct 30 – Nov 1 | Desktop avatar, widget builder, Muse Image expressions |
| Nov 2 | Ray-Ban Meta glasses path, SAM 3.1 camera check-ins |
| Nov 3–4 | Polish, demo video, submit |

See `BUILD_LOG.md` for what was built when.
