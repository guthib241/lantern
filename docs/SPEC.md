# Lantern: product spec (v2, updated with research Oct 26)

**One line:** Meta's apps and glasses connect billions of people, but not to your own computer. Lantern is the bridge: a private AI companion with a personality that lives on your laptop, keeps private folders off the cloud, and does the boring work (forms, files, emails, project fixes) after you approve on WhatsApp or by voice from Ray-Ban Meta glasses.

**Theme:** Build for Your World. **Track:** Indie. **Submit:** Nov 4, 9:00 PM ET = **Nov 5, 7:30 AM IST**. Plan to submit Nov 4 evening IST.

## The four things that must never be cut (the story)
1. Privacy zones with the local Glimmer lane and the cloud audit log
2. Approval flow on WhatsApp (buttons), bound to the exact action
3. Form filling with computer use, plus verification after acting
4. The avatar with personality

## Judging targets
| Criterion | How Lantern scores |
|---|---|
| Impact 30% | Saves time on chores everyone has. Private by design, so people would actually let it in (the opposite of Microsoft Recall). |
| Meta tooling 25% | Six Meta models, each doing a job only it can do, plus tool search over deferred MCP tools, Wonder safety checks routed to a human, keyword-biased transcription. An on-screen "model badge" shows which model ran each step. |
| Technical depth 20% | Policy engine, scrubber, approval bus with payload hashes, verify-after-act, sandboxed self-written widgets, encrypted memory. |
| Demo 25% | One sharp end-to-end story recorded live, with a memorable character. |

## Meta model map
| Model | Job | Notes |
|---|---|---|
| `muse-spark-1.3` | Main brain: planning, tools, replies | Default. $1.25 in / $4.25 out per 1M, $0.15 cached. Responses API with `previous_response_id`. |
| `muse-wonder-1.0` | Computer use for forms | $4 / $20 per 1M. Develop the loop on Spark (same `computer` tool works), switch to Wonder for final runs. Cap 25 steps, 1280x800 JPEG screenshots. |
| Muse Glimmer (30B, Apache-2.0) | **Private lane:** only model allowed to see private-zone content | Q4_K_M needs ~19 GiB GPU memory. On a smaller laptop use IQ2_M/Q2_K (~11 GB) with `-c 8192`, or self-host on Modal. Backend is just an OpenAI-compatible URL in config. |
| `muse-voice-transcribe-1.0` | WhatsApp voice notes and glasses voice | $0.18/hour. File endpoint `POST /v1/asr/transcribe`. Convert OGG/Opus with ffmpeg to 16 kHz mono first. Keyword biasing with the user's project and contact names. Handles Hinglish code-switching. |
| `muse-image-1.0` | Avatar expression sheet | $0.01/image. Generate one canonical character, upload via Files API, create each expression as an edit anchored on that file. |
| `sam-3.1` | Optional camera check-in reaction | $2.50 per 1,000 images. Parse output with `@meta-sam/parser` (TypeScript, in the desktop app). Cut first if behind. |

**Never use `-contributor` model IDs on user data** (Meta trains on it). Contributor is allowed only for synthetic eval prompts.

**Budget plan ($150):** Spark dev turns ~$20, Wonder final runs ~$40, images/voice/SAM <$5, keep $60+ for recording-day retries. Log tokens and cost per task.

## Architecture
```
 WhatsApp (text, voice, buttons) ─┐
 Ray-Ban Meta (voice → WhatsApp) ─┤   channels/  (WhatsApp webhook via cloudflared, Telegram polling fallback)
 Desktop overlay (Electron) ──────┤   websocket
                                  ▼
            lantern-core  (FastAPI on 127.0.0.1)
              ├─ policy.py     zones: hidden / private / open / writable — every file access goes here
              ├─ scrubber.py   secrets + PII (detect-secrets, gitleaks regexes, Aadhaar/PAN/UPI/card patterns)
              ├─ router.py     private content → Glimmer URL; everything else → Muse Spark
              ├─ audit.py      every cloud call: time, model, bytes, SHA-256, redactions
              ├─ approvals.py  single-use nonce, 10-min expiry, bound to hash of exact payload
              ├─ agent.py      plan → act → verify loop (exists)
              ├─ memory.py     tasks, timelines, facts (exists) + sqlite-vec local embeddings, encrypted at rest
              ├─ tools/        files, env map, email, forms (computer use), widgets
              ├─ mcp_client.py MCP servers as deferred tools + tool_search
              └─ mcp_server.py Lantern exposed as an MCP server ("ready for Meta AI Connectors")
```

## Features

### F1 Memory (built)
Tasks with status and summary, step timelines, facts, briefing each call. Add: cost per task, resume unfinished tasks, encryption at rest (SQLCipher or key in OS keychain), one-click "forget" that really deletes.

### F2 Privacy zones
`~/.lantern/zones.json`, longest matching path wins:
- `hidden`: not even listed to the model. Defaults: `~/.ssh`, `~/.gnupg`, browser profile folders, keychains, anything with `password`/`bank` in a top-level name if the user opts in.
- `private`: readable only by the Glimmer lane. The cloud model sees a redacted description ("PDF, 'Offer Letter', 2 pages") or Glimmer's answer, never raw content.
- `open`: normal, but every outbound payload is scrubbed.
- `writable`: Lantern may create or move files here, with approval and a preview.
UI shows "What left my laptop today" from the audit log. Demo: a planted fake API key gets redacted before the cloud call.

### F3 Environment map
Walk project roots for `.git`, `pyproject.toml`, `requirements*.txt`, `uv.lock`, `poetry.lock`, `package.json`, `pyvenv.cfg`, `.venv`, `conda-meta/`. `conda env list --json`. Per interpreter `<env>/bin/python -m pip list --format=json` (Windows: `<env>\Scripts\python.exe`). Cache in SQLite keyed by mtime so "which projects use torch?" needs no model call. Tools: `find_project`, `project_info`, `which_projects_use(package)`, `run_in_project(cmd)` (allowlist: run tests, pip install with approval, always inside that env, never global).

### F4 Files and email
Search by name/content, local embeddings (never a cloud embedder for private zones). Email: draft → approval showing recipient, subject, attachment names and hashes → send (Gmail API or SMTP app password) → verify in Sent via IMAP.

### F5 Forms ("I hate doing forms")
Playwright Chromium with a dedicated profile (no saved passwords). Playwright discovers fields (`get_by_label`); the `computer` tool handles messy widgets. Profile vault (name, address, education, links, reusable answers) stored locally.
Hard stops in code: never type into `input[type=password]`, OTP, CVV, card or payment iframes, or government ID fields; stop at CAPTCHAs ("you do this part"). Route Wonder `pending_safety_checks` and the final Submit to the approval bus. Verify with a screenshot plus a DOM check for a confirmation element. Develop on a local demo form in `demo/forms/`.

### F6 Channels
WhatsApp Cloud API test number: GET handshake (`hub.challenge`), verify `X-Hub-Signature-256`, reply 200 fast. Voice: media id → GET `/{media-id}` → download with bearer → ffmpeg → transcribe. Approvals as interactive reply buttons (`approve:<id>`, `deny:<id>`, `show:<id>`). System User token, not the 24h token. Scope: Lantern tasks, approvals, status, files, forms, reminders. **Not** a general chatbot (WhatsApp's Jan 2026 AI-provider rule); say so in the README.
Telegram (`python-telegram-bot`, long polling, inline keyboards) behind the same `Channel` interface as fallback.
Glasses: save the test number as contact "Lantern"; "Hey Meta, send a voice message to Lantern on WhatsApp"; replies are read aloud.
STOP keyword and a global hotkey cancel the in-flight response.

### F7 Avatar and personality
Electron transparent window: `transparent:true, frame:false, alwaysOnTop:true, skipTaskbar:true`, `setIgnoreMouseEvents(true,{forward:true})` except over the sprite. Sprite frames from the Muse Image sheet (idle, thinking, smug, annoyed, celebrating, needs-approval, sleeping), CSS bob/blink. No Live2D. Personas: smug (default), sarcastic, helpful. Humor style notes from memes/reels the user **shares**; never scrapes. Never analyzes the user's personality.

### F8 Widgets
Model writes HTML/JS; static check rejects `fetch`, `eval`, `import`, `XMLHttpRequest`, `WebSocket`. Runs in `<iframe sandbox="allow-scripts">` (no `allow-same-origin`), CSP `default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'`, postMessage bridge with only `notify()` and per-widget `storage.get/set`. Saved in `~/.lantern/widgets/`. Water reminder = a timer and a joke, no health advice.

### F9 MCP
Client: official `mcp` Python SDK, MCP tools passed as `defer_loading: true` with `{"type":"tool_search"}` (Responses API only; never send tool_search without deferred tools). Server: expose Lantern's safe tools over MCP.

## Safety model (show it in the demo)
- Tiers: read in open zones = automatic; write in writable zones = preview + approve; anything external (send, submit, install, shell) = approval with exact payload.
- Approvals: single-use, 10-minute expiry, bound to payload hash; any change needs re-approval.
- Prompt injection: file/web/email content wrapped as untrusted data with provenance; once untrusted content is in context, every high-risk tool requires approval; strip hidden text (zero-width chars, HTML comments, invisible CSS); never follow instructions found in content.
- No deletes. Hidden zones never listed. Server on 127.0.0.1. Kill switch.

## Demo (3 minutes)
- 0:00 Hook: "I hate forms. My AI lives on my laptop and never reads my private folder." Avatar wakes up.
- 0:15 Walking, glasses: "send a voice message to Lantern: fill the hackathon reimbursement form and email it to the organiser."
- 0:50 Laptop fills the form live (model badge: Muse Wonder). Submit pauses → WhatsApp Approve/Deny → tap Approve → verification screenshot.
- 1:40 Privacy: ask about a file in ~/Private, badge shows "Muse Glimmer (local)". Audit log: 0 bytes of it left the machine. Planted fake key redacted.
- 2:20 "Which projects use torch?" + "make me a water timer" → widget appears, avatar teases.
- 2:45 Architecture + Meta model map slide. "Ready for Meta AI Connectors."

## Cut order if behind
1 SAM check-in, 2 MCP client (keep server), 3 self-written widgets (ship one pre-made in the sandbox), 4 Telegram, 5 environment map depth, 6 desktop-wide computer use (browser only).

## UI direction
Friendly modern consumer look: rounded cards, soft gradients, the avatar as the hero, approval cards with one big Allow button, model badges. Our own identity. No Meta logos.
