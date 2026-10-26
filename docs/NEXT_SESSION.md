# Handoff: everything left to build (M7 → M17)

State at handoff (Oct 26): M0–M6 done, 93 offline tests passing, PR https://github.com/guthib241/lantern/pull/1 on branch `claude/gallant-albattani-18fmje`.

## Working rules for the next session
- **Keep working through M7 → M17 in order without stopping.** Do not end your turn after a milestone. Only stop when the user says **"compact"** (or every milestone is done).
- After **each** milestone: `python -m pytest -q` green → tick `docs/MILESTONES.md` → append `BUILD_LOG.md` → decisions into `docs/DECISIONS.md` → new libraries into `docs/THIRD_PARTY.md` + `requirements.txt` → commit → `git push -u origin <branch>` → post a 3-line note (what works / how to try / what to set up), then go straight on to the next milestone.
- When the user says **"compact"**: finish or revert the half-done step so tests are green, commit, push, update the PR (or open one) with a summary of each milestone, and reply with a handoff prompt like this file (also update this file).
- Ask the user only about decisions that change the product. Everything else: pick the simpler option that makes the 3-minute demo reliable, write it in `docs/DECISIONS.md`, continue.
- Follow `CLAUDE.md` strictly: new code only, Meta models only, no secrets in git, never call the real API in tests, no Meta logos, no health/medical advice, never analyze the user's personality.
- Before writing code against a Meta API feature you haven't used yet, read its page under https://dev.meta.ai/docs/ and the cookbook (`git clone --depth 1 https://github.com/meta-models/meta-model-cookbook` into a scratch dir; read only, don't copy code). If a page can't be reached, say so and use shapes already in the repo.
- Anything needing the laptop or an account (WhatsApp, Telegram, Gmail, Modal, Baseten, webcam, Electron window) goes behind a config flag with a **fake mode** so it builds and tests in the cloud. Tell the user exactly what to set up.

## Architecture already in place (reuse, don't rebuild)
| File | What it gives you |
|---|---|
| `lantern/models.py` | model IDs, prices, `badge()`, `check_model()` (refuses `-contributor`), `Usage`, `token_cost/image_cost/audio_cost/sam_cost` |
| `lantern/policy.py` | **only module allowed to do file I/O** (`read_text/read_bytes/write_text/write_bytes/move/list_dir/walk/describe_file`, zones hidden/private/open/writable). A test fails if other modules use `os.walk`, `iterdir`, `shutil.move/copy`, `unlink`, `os.remove`, `rmtree`. Lantern's own files under `~/.lantern` (config, token, media, widgets) may be read directly. |
| `lantern/scrubber.py`, `lantern/audit.py` | `GuardedClient(client, audit, lane=cloud/local/selfhosted)` scrubs + SHA-256 + logs every call; `AuditLog.record(...)` for non-OpenAI calls (voice, WhatsApp media, SMTP) |
| `lantern/router.py` | private lane (`ask_private`), `FakeGlimmer` default, `OpenAIGlimmer` for real |
| `lantern/approvals.py` | `ApprovalBus` (nonce, 10-min expiry, payload hash, single use, `subscribe`, `cancel_all`), `CallbackGate`, `BusGate`. In tools: `commit = self._approve(action, summary, payload)`; `None` = declined; right before acting `if refused := self._commit(commit, payload): return refused`. Payload = compact description of the effect (hashes, not raw content). |
| `lantern/untrusted.py` | `wrap(text, source, kind)` for any file/web/email/MCP content; set `toolbox.tainted = True` whenever it enters context |
| `lantern/tools.py` | `Toolbox`, `TOOL_SPECS` (`_fn(name, desc, props, required)`), `call()` dispatch via `TOOL_NAMES`. Keep the tool list byte-stable (prompt caching): add new tools at the end, always present (error if the feature isn't configured). Function names: at most one dot. |
| `lantern/agent.py` | `Agent.run(goal, task_id=None)`, `agent.cancel` Event, events `task/tool/usage/done`, `tool_badge()` (a tool result with `"model"` key sets the step badge) |
| `lantern/core.py` | `Core(cfg, client)`: worker thread, `submit(goal)`, `emit(event)`, `subscribe(sink)`, `stop()` (kill switch), `wait_idle()` |
| `lantern/server.py` | FastAPI on 127.0.0.1, `X-Lantern-Token` from `~/.lantern/token`, websocket `/events?token=` |
| `lantern/voice.py` | `transcribe(path, keywords=..., post=...)`, `to_wav16k()` (ffmpeg) |
| `tests/fakes.py` | `FakeClient(script)` with items `(output, text)` or `(output, text, usage)`, `call(name, cid, **args)`, `usage(inp, cached, out)` |

## Meta API facts already verified (from dev.meta.ai docs / cookbook, Oct 26)
- Base `https://api.meta.ai/v1`, `Authorization: Bearer $MODEL_API_KEY`, OpenAI Python SDK works.
- Prices: Spark $1.25 in / $0.15 cached / $4.25 out per 1M; Wonder $4 / $0.20 / $20; Image $0.01/image; Voice $0.18/hour; SAM $2.50 per 1k images.
- Response `usage`: `input_tokens`, `input_tokens_details.cached_tokens`, `output_tokens`, `output_tokens_details.reasoning_tokens`. Output item types include `message`, `function_call`, `computer_call` (`call_id`, `action`/`actions`, `pending_safety_checks`, `status`), `reasoning`, `tool_search_call`, `image_generation_call`.
- Muse Image: `responses.create(model="muse-image-1.0", input=prompt)`; image is base64 in `output[i].result` where `type == "image_generation_call"`. Edits/refinements: chained turn with `previous_response_id`. Reference images: `input=[{"role":"user","content":[{"type":"input_text",...},{"type":"input_image","image_url":"data:..."}]}]` (must be wrapped in a user message).
- Voice: `POST /asr/transcribe` multipart `request` (JSON `{"model","mode":"PUSH_TO_TALK","audioEncoding":"WAV","keywords"?,"languageBias"?}`) + `audio` (mono 16-bit WAV 16/24 kHz). Max 10 min / 32 MB.
- SAM 3.1: `responses.create(model="sam-3.1", stream=True, input=[{"type":"message","role":"user","content":[{"type":"input_text","text":"<noun phrase>"},{"type":"input_image","image_url":"data:image/...;base64,..."}]}])`; parse with `meta_sam_parser` (Python) / `@meta-sam/parser` (TS). One noun phrase per request.
- Glimmer: OpenAI chat completions, model `muse-glimmer`, temperature 1.0 / top_p 0.95 / top_k 64 (never 0), `chat_template_kwargs.reasoning_strength` low|medium|high|xhigh, one tool call per turn.
- **Not read yet:** `/docs/computer-use` (M7), `/docs/tool-search` (M14), `/docs/file-handling` + `/docs/image-generation` + cookbook `05_muse_image/03_anchored_generation` (M11), `/docs/error-handling` (M17). Read them first.

## Environment notes (cloud sandbox)
- Python 3.13, Node 22, ffmpeg installed. `pip install` works.
- Python Playwright is **not** installed: `pip install playwright`, then launch Chromium with `executable_path="/opt/pw-browsers/chromium"` (never run `playwright install`). Node `require('playwright')` already works.
- Tests must stay offline and fast. Skip browser tests cleanly if Chromium is missing (`pytest.importorskip` / skip marker), but they should run in the cloud.

---

## M7 ☁ Forms (computer use) — one of the 4 "never cut" features
1. Read `/docs/computer-use` and cookbook `03_use_cases/12_computer_use` + `13_macos_cua` for the exact `computer` tool definition, `computer_call` actions, `computer_call_output` (screenshot format, `acknowledged_safety_checks`) and step/screenshot guidance. Spec says: develop on `muse-spark-1.3`, switch to `muse-wonder-1.0` for final runs, cap 25 steps, 1280x800 JPEG screenshots.
2. `demo/forms/reimbursement.html` (+ `confirmation.html`): our own friendly hackathon reimbursement form: name, email, event, date, amount, purpose, receipt upload, a custom date picker or dropdown widget (messy widget for the `computer` tool), plus trap fields to prove hard stops: password, OTP, card number/CVV, Aadhaar/PAN, and a fake CAPTCHA box. Submit shows a confirmation page with `#confirmation` and a reference number.
3. `lantern/forms/vault.py`: local profile vault (`~/.lantern/profile.json`: name, email, phone, address, education, links, reusable answers). Never stores passwords/card numbers.
4. `lantern/forms/driver.py`: Playwright driver with a dedicated profile dir `~/.lantern/browser-profile` (no saved passwords). `open(url)` (allowlist: `file://` demo forms + domains in config), `discover_fields()` via labels (`get_by_label`, `aria-label`, `placeholder`) → `[{label, name, type, autocomplete, selector}]`, `fill(selector, value)`, `screenshot()` (1280x800 JPEG), `click(x,y)`, `type(text)`, `keypress(keys)`, `scroll`, `wait`, `dom_has(selector)`.
5. **Hard stops in code** (`lantern/forms/guards.py`): never type into `input[type=password]`, `autocomplete` `one-time-code` / `cc-*`, names/labels/ids matching `otp|one.?time|cvv|cvc|card|pin|aadhaar|pan\b|passport|ssn|password`, inputs inside payment iframes (stripe/razorpay/paypal/adyen/braintree), and stop at CAPTCHAs (recaptcha/hcaptcha/turnstile iframes or "captcha" text) with "you do this part". For coordinate clicks/typing from the `computer` tool, check the element under the point / focused element before typing.
6. `lantern/forms/computer.py`: the loop: screenshot → model (`computer` tool) → action → screenshot back. Fill known fields directly via Playwright first (from the vault), use the `computer` tool for messy widgets. `pending_safety_checks` → approval bus (acknowledge only if approved). **Final Submit always goes through the approval bus**, payload = url + sha256 of each field value + field labels; recompute right before clicking. After submit: verify with DOM check for the confirmation element + save screenshot to `~/.lantern/media/`; return `verified: true/false`.
7. Tool `fill_form(url, goal)` in the toolbox (badge "Muse Wonder" or "Muse Spark" via result `model`). Screenshots counted in usage/cost; audit logs bytes.
8. Tests: Playwright against the demo form with a fake model scripting `computer_call`s; hard-stop tests (password/OTP/card/ID/CAPTCHA refused); Submit blocked until approved; changed field after approval → refused; verification true on confirmation page, false otherwise.

## M8 ☁/💻 Channels (WhatsApp + Telegram + STOP)
1. Read the WhatsApp Cloud API docs (developers.facebook.com/docs/whatsapp/cloud-api: webhooks, messages, interactive reply buttons, media) before coding.
2. `lantern/channels/base.py`: `Channel` interface: `send_text(text)`, `send_approval(approval)` (Approve / Deny / Show buttons), `send_voice_reply?` (skip), `handle_incoming(...)`. Only the owner can talk to Lantern: allowlist `LANTERN_WHATSAPP_OWNER` (phone) / `LANTERN_TELEGRAM_OWNER` (chat id); everything else ignored.
3. `lantern/channels/whatsapp.py` + routes mounted on the FastAPI server (exempt from the token, protected by signature): `GET /webhook/whatsapp` handshake (`hub.mode`, `hub.verify_token`, return `hub.challenge`); `POST` verifies `X-Hub-Signature-256` (HMAC-SHA256 of the raw body with the app secret, constant-time compare), replies 200 fast and processes in the background. Text → `core.submit`. Button reply `approve:<id>` / `deny:<id>` / `show:<id>` → `bus.decide` (server looks up the nonce; the channel is trusted after signature + owner check). Voice note: media id → `GET /{media-id}` → download URL with bearer → save in `~/.lantern/media` → `to_wav16k` → `voice.transcribe(keywords=contact + project names from memory facts)` → submit; audit-log the upload. Send via `POST https://graph.facebook.com/<version>/<phone_number_id>/messages`. Button titles ≤ 20 chars, max 3 buttons. Core events → channel: approval requested → buttons; task done → reply text (in persona).
4. **STOP** keyword (any channel) and `/stop` → `core.stop()`.
5. `lantern/channels/telegram.py`: fallback via raw Bot API long polling (`getUpdates`, `sendMessage` with inline keyboard, `answerCallbackQuery`) using stdlib/httpx, no extra dependency.
6. `lantern/channels/fake.py` for tests. Config: `LANTERN_CHANNEL=fake|whatsapp|telegram`, `WHATSAPP_TOKEN` (System User token, not the 24h one), `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_GRAPH_VERSION`, `TELEGRAM_BOT_TOKEN`.
7. README scope note: WhatsApp is a personal task/approval console for Lantern, **not** a general chatbot (WhatsApp's Jan 2026 AI-provider rule).
8. Tell the user the setup steps: Meta developer app → WhatsApp product → test number → add own number as recipient → System User permanent token with `whatsapp_business_messaging` → `cloudflared tunnel --url http://127.0.0.1:8765` → webhook URL `https://<tunnel>/webhook/whatsapp` + verify token → subscribe to `messages`. Glasses: save the test number as contact "Lantern", "Hey Meta, send a voice message to Lantern on WhatsApp".
9. Tests: handshake, bad signature rejected, non-owner ignored, text → task, button → approval decided, voice → fake download + fake transcribe → task, STOP cancels, Telegram callback flow with a fake HTTP layer.

## M9 ☁ Email
1. `lantern/email_tool.py`: tool `send_email(to, subject, body, attachments=[], cc=[])`. Attachments read through `policy` (private-zone attachments allowed only with the approval card marking them PRIVATE). Approval payload: to, cc, subject, sha256(body), attachment names + sha256 + sizes. Card warns if a recipient appeared only in untrusted content (taint).
2. Send: `smtplib` SSL (Gmail: smtp.gmail.com:465 + app password). Set a `Message-ID`. Verify: IMAP (`imaplib`, Gmail `"[Gmail]/Sent Mail"`) search `HEADER Message-ID <id>` → `verified`. Audit-log bytes + sha256 (lane "email").
3. Config: `LANTERN_EMAIL_MODE=fake|smtp`, `LANTERN_SMTP_HOST/PORT/USER/PASSWORD`, `LANTERN_IMAP_HOST`, `LANTERN_SENT_FOLDER`. Fake SMTP/IMAP classes in tests.
4. Tests: declined → nothing sent; body changed after approval → refused; verified true when found in fake Sent, false otherwise; hidden-zone attachment refused.

## M10 ☁/💻 Desktop overlay (Electron) — "never cut" #4 (avatar with personality)
1. `desktop/` with `package.json` (electron), `main.js`, `preload.js` (`contextIsolation: true`, `nodeIntegration: false`, exposes token + API base via `contextBridge`), `renderer/`.
2. Avatar window: `transparent:true, frame:false, alwaysOnTop:true, skipTaskbar:true`, `setIgnoreMouseEvents(true,{forward:true})` except over the sprite; CSS bob/blink; expressions idle / thinking / smug / annoyed / celebrating / needs-approval / sleeping driven by events. Placeholder SVG sprites until M11.
3. Panel window: chat box (POST /task), live step list with **model badges**, approval cards (one big Allow button + Deny + details; shows payload summary and "after reading untrusted content"), audit view "What left my laptop today" (cloud vs local vs self-hosted bytes, redactions), zones editor, usage/cost, widget iframe host (`sandbox="allow-scripts"`, no `allow-same-origin`, CSP), STOP button + global hotkey (kill switch).
4. Friendly modern look: rounded cards, soft gradients, our own identity, **no Meta logos**.
5. Fake mode: `npm run mock` = small mock event server (or `python -m lantern --serve --demo` once M17 exists). Keep logic in pure JS modules (event reducer, expression picker, approval card model) tested with `node --test`. Optional Playwright-Electron smoke test if it runs headless.
6. Tell the user: `cd desktop && npm install && npm start` on the laptop.

## M11 ☁ Muse Image expression sheet
1. Read `/docs/image-generation`, `/docs/file-handling`, cookbook `05_muse_image/03_anchored_generation`. Pick the documented way to anchor edits on one canonical character (Files API upload + edits, or `previous_response_id` chaining) and record why.
2. `scripts/make_avatar.py`: generate one canonical lantern character (our own design), then each expression as an edit anchored on it → `desktop/assets/avatar/<expression>.webp` + `manifest.json`; log cost ($0.01/image) to memory usage; `--dry-run` without a key.
3. Tests with a fake client returning `image_generation_call` base64; verify files written and anchoring parameter used.

## M12 ☁ Environment map + project tools
1. `lantern/envmap.py`: walk roots **via policy** for `.git`, `pyproject.toml`, `requirements*.txt`, `uv.lock`, `poetry.lock`, `package.json`, `pyvenv.cfg`, `.venv`, `conda-meta/`; parse declared deps; `conda env list --json` if conda exists; per interpreter `<env>/bin/python -m pip list --format=json` (Windows `Scripts\python.exe`); cache in SQLite keyed by path + mtime so "which projects use torch?" needs no model call.
2. Tools: `find_project(name)`, `project_info(path)`, `which_projects_use(package)`, `run_in_project(path, command)`: allowlist only (`pytest`, `python -m pytest`, `npm test`, `pip install <pkg>` with approval), always that project's env interpreter, never global, no `shell=True`, timeout, output wrapped as untrusted.
3. Tests on fake project folders (fake venv with a stub python script).

## M13 ☁ Widgets
1. `lantern/widgets.py`: tool `make_widget(name, description)`: Spark writes one self-contained HTML file; **static checker** rejects `fetch`, `eval`, `Function(`, `import`, `XMLHttpRequest`, `WebSocket`, `Worker`, `navigator.sendBeacon`, `<script src`, `<link`, `<iframe`, `http(s)://` URLs, `document.cookie`, `top.`/`parent.` except the bridge; on reject, one retry with the errors, then fail. Save to `~/.lantern/widgets/<name>.html` after approval.
2. Sandbox contract (`docs/WIDGETS.md`): iframe `sandbox="allow-scripts"`, CSP `default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'`, postMessage bridge with only `notify(text)` and per-widget `storage.get/set`.
3. Pre-made `widgets/water_timer.html`: a timer and a joke. **No health advice.**
4. Tests: checker rejects each banned construct, accepts the water timer; generation with fake client.

## M14 ☁ MCP client + Lantern MCP server
1. Read `/docs/tool-search`. Use the official `mcp` Python SDK.
2. Client (`lantern/mcp_client.py`): servers from `~/.lantern/mcp.json`; their tools passed as function tools with `defer_loading: true` plus `{"type": "tool_search"}`; **never send `tool_search` without at least one deferred tool**; `tool_choice` stays "auto"; names at most one dot (e.g. `server.tool`). Results are untrusted (wrap + taint); each MCP call needs approval unless the user allowlisted it as read-only.
3. Server (`lantern/mcp_server.py`, stdio): exposes safe tools (`list_folders`, `search_files`, `read_file` for open zones, `ask_private`, `task_status`, `submit_task`); anything that changes things goes through the approval bus. README: "ready for Meta AI Connectors".
4. Tests: request contains deferred tools + tool_search only when deferred tools exist; server lists tools and refuses hidden paths (in-process transport).

## M15 ☁ Memory encryption at rest + real "forget"
1. Encrypt sensitive columns (task goal/summary, step content, fact values) with `cryptography` Fernet; key in the OS keychain via `keyring`, fallback `~/.lantern/key` (mode 600). Migration for existing plaintext rows. (SQLCipher is painful on Windows; record the decision.)
2. Real forget: `forget_fact`, `forget_task(id)` (deletes task, steps, usage, its audit rows), `forget_everything`; `PRAGMA secure_delete=ON` + `VACUUM`. CLI flags + server routes + desktop buttons.
3. Tests: raw DB file contains no plaintext; forget really removes bytes from the file.

## M16 ☁ SAM 3.1 check-in (optional, cut first if behind)
1. `lantern/sam.py`: segment one webcam snapshot for a noun phrase (e.g. "coffee mug", "laptop") using the SAM shape above; `meta_sam_parser` in Python (or `@meta-sam/parser` in desktop). Cost logged.
2. Avatar reacts lightly ("ah, coffee. good."). No analysis of the user's face, emotions, health or personality. Webcam only on explicit user action; desktop captures, nothing stored.
3. Fake mode + tests with a fake stream.

## M17 ☁ Reliability + demo mode + README
1. Retries with exponential backoff on 429/5xx/timeouts (read `/docs/error-handling`), timeouts everywhere, clear offline errors, agent survives a dropped connection mid-task.
2. `--demo` rehearsal mode: a scripted `DemoClient` replays the full 3-minute story offline with no API spend, against `demo/home/` (Documents, Downloads, `Private/offer_letter.txt`, a planted fake key file) and `demo/forms/`: voice-style goal → fill form → approval → verification → private question answered by Glimmer lane → audit shows 0 private bytes + redacted key → "which projects use torch?" → water timer widget. `python -m lantern --serve --demo` drives the desktop app too.
3. README rewrite: one-line pitch, mermaid architecture diagram, Meta model map (which model does which job), safety model, WhatsApp scope note, setup in ≤ 5 commands, demo script, cost so far.
4. Final pass: `python -m pytest -q`, `scripts/smoke.py` instructions, BUILD_LOG summary, PR.

## What the user must set up (collect these in the final message)
- `MODEL_API_KEY` in `.env` → `python scripts/smoke.py`.
- Laptop: llama.cpp serving Glimmer IQ2_M (see `docs/DECISIONS.md`) → `LANTERN_GLIMMER_URL=http://127.0.0.1:8080/v1`.
- WhatsApp Cloud API app + test number + System User token + cloudflared tunnel (M8 list above).
- Gmail app password for SMTP/IMAP (M9).
- `cd desktop && npm install && npm start` (M10).
