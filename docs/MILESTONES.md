# Milestones (in order; tick, log, commit after each)

Tags: ☁ can be fully built and tested in Claude Code on the web. 💻 final step needs the user's laptop (build it with a fake/offline mode so tests pass in the cloud).

- [x] M0 Oct 26 — Agent loop, memory, sandboxed file tools, CLI, tests
- [x] M1 ☁ `models.py` registry + cost table; token/cost logging per task; `scripts/smoke.py` (one real call per model, prints OK/fail, skipped if no key)
- [x] M2 ☁ `policy.py` zones (hidden/private/open/writable) + `zones.json`; all tools go through it; hidden paths never listed; tests
- [x] M3 ☁ `scrubber.py` (secret + PII patterns incl. Aadhaar, PAN, UPI, card numbers) + `audit.py`; every cloud payload scrubbed and logged with SHA-256; tests with planted fake keys
- [x] M4 ☁/💻 `router.py`: private content → Glimmer OpenAI-compatible URL (config), cloud gets only redacted description or Glimmer's answer; fake local backend for tests
- [x] M5 ☁ `approvals.py`: pending approvals with nonce, 10-min expiry, payload hash; agent waits asynchronously; untrusted-content taint forces approval on high-risk tools
- [x] M6 ☁ FastAPI server on 127.0.0.1: `/task`, `/tasks`, `/approvals`, websocket events with model badge per step
- [x] M7 ☁ Forms: Playwright driver + `computer` tool loop (develop on `muse-spark-1.3`), local demo form in `demo/forms/`, hard stops for password/OTP/card/ID/CAPTCHA, Submit → approval, verify confirmation. Tests with Playwright against the demo form and a fake model
- [x] M8 ☁/💻 Channels: `Channel` interface; WhatsApp webhook (signature check, buttons, voice → ffmpeg → Voice Transcribe); Telegram fallback; STOP keyword. Fake channel for tests
- [x] M9 ☁ Email: draft → approve (recipient, subject, attachment hashes) → send → verify via IMAP; fake SMTP in tests
- [x] M10 ☁/💻 Desktop overlay (Electron): avatar window, chat, approval cards, audit view, zones editor, model badges, widget iframe host
- [x] M11 ☁ Muse Image expression sheet script (canonical + edits via Files API) → `desktop/assets/avatar/`
- [x] M12 ☁ Environment map + project tools, tested on fake project folders
- [x] M13 ☁ Widgets: generator, static checker, sandbox contract; one pre-made water timer
- [x] M14 ☁ MCP client (deferred tools + tool_search) and Lantern MCP server
- [x] M15 ☁ Memory encryption at rest + real "forget"
- [x] M16 ☁ SAM 3.1 check-in (optional, cut first)
- [x] M17 ☁ Reliability: retries, timeouts, offline mode, `--demo` rehearsal mode; README with diagram, model map, WhatsApp scope note, setup in under 5 commands

## Extras after M17 (requested: "complete it end to end, add features")
- [x] M18 ☁ Reminders: `set_reminder` / `list_reminders` / `cancel_reminder`, encrypted storage, a scheduler that pings every phone channel and the desktop (late ones say so), daily/weekly repeats
- [x] M19 ☁ Tidy a folder: `organize_files` moves many files under one approval bound to the whole plan, each move verified
- [x] M20 ☁ Resume after restart: tasks cut off by a crash or restart are marked interrupted, reported, and can be resumed
- [x] M21 ☁/💻 `lantern --doctor`: one checklist of everything the laptop needs, with the exact fix for each item (CLI, API, desktop)
- [x] M22 ☁ Packaging and CI: `pyproject.toml` with a `lantern` command, GitHub Actions running every test

## Design pass (requested: Apple-quality design, innovative and useful)
- [x] M23 Apple-style design system and panel redesign: system colours with light/dark, translucent toolbar and tab bar, large titles, inset grouped lists, approval sheet with a field table, a Today view with at-a-glance tiles and one-tap suggestions
- [x] M24 Live activity pill: Dynamic-Island-style status for the avatar with inline Allow / Deny
- [x] M25 Quick Ask: Spotlight-style bar on a global hotkey, native vibrancy / acrylic
- [x] M26 Native integration: menu bar icon and menu, notifications for approvals and reminders, badge count
- [x] M27 First-run welcome sheet and Preferences (personality picker saved next to memory, shortcuts list)
- [x] M28 Submission kit: shot-by-shot video script, architecture slide, write-up

## While waiting for the key (requested: "keep improving until I send the key")
- [x] M29 Undo: one tap (or "undo" typed or sent from the phone) puts back what a task moved. It runs from the task's own verified move log through one approval card, with no model call
- [x] M30 Spending limits enforced in code: total ($150), per task ($3) and optional daily, checked before every model call. Refused calls never leave and are never retried; a ledger that forgetting doesn't reset; Costs tab and `--doctor` show them
