# Milestones (in order; tick, log, commit after each)

Tags: ☁ can be fully built and tested in Claude Code on the web. 💻 final step needs the user's laptop (build it with a fake/offline mode so tests pass in the cloud).

- [x] M0 Oct 26 — Agent loop, memory, sandboxed file tools, CLI, tests
- [x] M1 ☁ `models.py` registry + cost table; token/cost logging per task; `scripts/smoke.py` (one real call per model, prints OK/fail, skipped if no key)
- [ ] M2 ☁ `policy.py` zones (hidden/private/open/writable) + `zones.json`; all tools go through it; hidden paths never listed; tests
- [ ] M3 ☁ `scrubber.py` (secret + PII patterns incl. Aadhaar, PAN, UPI, card numbers) + `audit.py`; every cloud payload scrubbed and logged with SHA-256; tests with planted fake keys
- [ ] M4 ☁/💻 `router.py`: private content → Glimmer OpenAI-compatible URL (config), cloud gets only redacted description or Glimmer's answer; fake local backend for tests
- [ ] M5 ☁ `approvals.py`: pending approvals with nonce, 10-min expiry, payload hash; agent waits asynchronously; untrusted-content taint forces approval on high-risk tools
- [ ] M6 ☁ FastAPI server on 127.0.0.1: `/task`, `/tasks`, `/approvals`, websocket events with model badge per step
- [ ] M7 ☁ Forms: Playwright driver + `computer` tool loop (develop on `muse-spark-1.3`), local demo form in `demo/forms/`, hard stops for password/OTP/card/ID/CAPTCHA, Submit → approval, verify confirmation. Tests with Playwright against the demo form and a fake model
- [ ] M8 ☁/💻 Channels: `Channel` interface; WhatsApp webhook (signature check, buttons, voice → ffmpeg → Voice Transcribe); Telegram fallback; STOP keyword. Fake channel for tests
- [ ] M9 ☁ Email: draft → approve (recipient, subject, attachment hashes) → send → verify via IMAP; fake SMTP in tests
- [ ] M10 ☁/💻 Desktop overlay (Electron): avatar window, chat, approval cards, audit view, zones editor, model badges, widget iframe host
- [ ] M11 ☁ Muse Image expression sheet script (canonical + edits via Files API) → `desktop/assets/avatar/`
- [ ] M12 ☁ Environment map + project tools, tested on fake project folders
- [ ] M13 ☁ Widgets: generator, static checker, sandbox contract; one pre-made water timer
- [ ] M14 ☁ MCP client (deferred tools + tool_search) and Lantern MCP server
- [ ] M15 ☁ Memory encryption at rest + real "forget"
- [ ] M16 ☁ SAM 3.1 check-in (optional, cut first)
- [ ] M17 ☁ Reliability: retries, timeouts, offline mode, `--demo` rehearsal mode; README with diagram, model map, WhatsApp scope note, setup in under 5 commands
