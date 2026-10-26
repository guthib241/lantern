# Build log

Everything in this repo was written during the Meta Global AI Developer Hackathon (Oct 26 – Nov 4, 2026).

## Oct 26
- Agent loop on `muse-spark-1.3` via the Responses API with function tools and a required `finish` tool
- SQLite memory: tasks, step timelines, facts; memory briefing injected into every call
- Sandboxed file tools (list, search, read, write, move) with approval prompts and on-disk verification
- CLI with chat mode, `--tasks`, `--task`, `--memory`, personas
- 16 offline tests with a scripted fake model
- Spec v2 from research: privacy zones, scrubber + audit log, approval bus, forms with computer use, WhatsApp/Telegram/glasses channels, Electron avatar, widgets sandbox, MCP client and server; 17 milestones; prompts for Claude Code on the web
