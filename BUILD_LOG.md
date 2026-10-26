# Build log

Everything in this repo was written during the Meta Global AI Developer Hackathon (Oct 26 – Nov 4, 2026).

## Oct 26
- Agent loop on `muse-spark-1.3` via the Responses API with function tools and a required `finish` tool
- SQLite memory: tasks, step timelines, facts; memory briefing injected into every call
- Sandboxed file tools (list, search, read, write, move) with approval prompts and on-disk verification
- CLI with chat mode, `--tasks`, `--task`, `--memory`, personas
- 16 offline tests with a scripted fake model
- Spec v2 from research: privacy zones, scrubber + audit log, approval bus, forms with computer use, WhatsApp/Telegram/glasses channels, Electron avatar, widgets sandbox, MCP client and server; 17 milestones; prompts for Claude Code on the web

## Oct 26 (Claude Code on the web)
- M1: `lantern/models.py` is the single home for model IDs, base URL, badges and the price table (from dev.meta.ai pricing page). Contributor-mode IDs are refused in code. Every Responses call logs input/cached/output tokens and USD cost per task (`usage` table); `--usage` and `--tasks` show spend. `lantern/voice.py` (stdlib multipart client for `/asr/transcribe`, per the voice API reference + cookbook). `scripts/smoke.py` makes one real call per model (Spark, Wonder, Image, Voice, SAM, optional Glimmer) and skips without a key.
