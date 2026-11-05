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
- M2: `lantern/policy.py` privacy zones (hidden / private / open / writable, longest match wins, `~/.lantern/zones.json`, always-hidden secret names, opt-in "bank/password" folder hiding). All file I/O in the toolbox now goes through it; hidden paths never listed or searched, private files return a redacted description, writes/moves only in writable zones. `--zones` / `--set-zone` in the CLI. A static test fails if any other module walks, moves or deletes files.
- M3: `lantern/scrubber.py` (private keys, AWS/GitHub/GitLab/Slack/Stripe/Google/HF/Meta keys, JWTs, bearer tokens, password assignments, URL passwords, Aadhaar w/ Verhoeff, PAN, UPI, Luhn-checked cards, SSN, IBAN) and `lantern/audit.py` (`AuditLog` + `GuardedClient`). Every cloud payload is scrubbed, hashed (SHA-256) and logged with byte count and redaction counts; local-lane calls are logged but not scrubbed. `--audit` shows "what left my laptop today". Test plants a fake Stripe key in a file and proves it never reaches the fake cloud.
- M4: `lantern/router.py`: private-zone files are read only by Muse Glimmer (OpenAI-compatible URL: llama.cpp locally or Modal), via the new `ask_private` tool; the cloud model sees only Glimmer's scrubbed answer and a redacted description. Lane-down never falls back to the cloud. `FakeGlimmer` for tests/cloud sandboxes; config via `LANTERN_GLIMMER_MODE/URL/MODEL/KEY`. Audit separates local / self-hosted / cloud bytes.
- M5: `lantern/approvals.py` (ApprovalBus: random nonce, 10-minute expiry, SHA-256 payload binding, single use, listeners, kill switch; `CallbackGate` for the terminal, `BusGate` for async approval from the desktop/WhatsApp). `lantern/untrusted.py` strips zero-width/bidi chars, HTML comments, CSS-hidden elements and scripts, and wraps file text with provenance. Toolbox writes/moves go through the bus; untrusted content taints the task and forces approval on otherwise-automatic tools.
- M6: `lantern/core.py` (wires memory, policy, audit, router, approval bus and agent; worker thread; event fan-out; kill switch) and `lantern/server.py` (FastAPI on 127.0.0.1 with token auth: `/task`, `/tasks`, `/approvals`, `/stop`, `/audit`, `/zones`, `/memory`, `/usage`, websocket `/events` with model badges). `python -m lantern --serve`. Agent gained cancel support, server-created task ids, a `done` event and per-step badges; memory is now thread-safe.

## Oct 26 (Claude Code on the web, session 2)
- M7: Forms with computer use (`lantern/forms/`). `guards.py`: hard stops in code (password, OTP, card/CVV/PIN, Aadhaar/PAN/passport/SSN, payment frames, CAPTCHA) on field metadata and on typed text. `vault.py`: local profile vault `~/.lantern/profile.json` (mode 600) that refuses secrets, card and ID numbers; CLI `--profile`, `--profile-set`, `--form-bookmark`; server `GET/POST /profile`. `driver.py`: Playwright Chromium with a dedicated profile (password manager and autofill off), 1280x800, label-based field discovery, an in-page submit blocker. `computer.py`: the screenshot → model → actions loop, max 25 steps, two protocols (Meta's native `computer` tool with `computer_call`/`computer_call_output` and safety checks routed to the approval bus, and the cookbook's `computer.computer`/`computer.stop` function tools as an automatic fallback). `filler.py`: discover → plan values (Spark, `set_values`) → DOM fill → computer use for custom widgets → Submit approval bound to hashes of every value → verify confirmation on the page + screenshot in `~/.lantern/media`. New `fill_form` tool (appended to the tool list). Demo pages in `demo/forms/` (reimbursement form with a custom dropdown and trap fields, confirmation page, trap page with CAPTCHA and a payment frame). `scripts/smoke.py cua` calibrates protocol and coordinates against the real API. Scrubber leaves base64 screenshots alone. 63 new tests (18 drive real Chromium); 156 passing.
- M8: Channels (`lantern/channels/`): one `Channel` interface, a `ChannelHub` with inbox/outbox threads, and three channels that can run together. WhatsApp Cloud API webhook (`GET/POST /webhook/whatsapp`: handshake, `X-Hub-Signature-256` check on raw bytes, 200 at once, retries de-duplicated), owner allowlist, approval cards with Approve / Deny / Show buttons whose ids carry the nonce and a payload-hash prefix, voice notes (media download → ffmpeg 16 kHz WAV → Muse Voice Transcribe with keyword biasing from memory and the profile vault, audited and costed). Telegram fallback over the raw Bot API with long polling. The simulated phone (`fake`) has `/channel/messages` and `/channel/fake` routes for the desktop and demo mode. STOP from any channel (typed or spoken) is the kill switch; `status` and `help` replies. Outgoing text is scrubbed and audited. `python -m lantern --channel-test "hi"` checks the phone side. 19 new tests; 175 passing.
- M9: Email (`lantern/email_tool.py`): new `send_email` tool (appended to the tool list). Recipients are validated; attachments go through policy (hidden refused, private flagged PRIVATE). The approval card shows recipients, subject, a body preview, attachment names and sizes, and warnings (recipient only seen in untrusted content, secret-looking body, private files); the bound payload holds hashes only and is rebuilt from disk before sending. SMTP over SSL sends with a Message-ID, IMAP finds it in Sent to set `verified`, and the send is audited (lane cloud, model "email"). Fake mailer by default. 10 new tests; 185 passing.
- M10: Desktop app (`desktop/`, Electron 44.5.1).
  - **Avatar window:** transparent, always on top, click-through except over the sprite. Expressions follow the event stream. A speech bubble shows the latest reply or "I need your OK". Right-click menu: STOP and Quit.
  - **Panel:** approval cards with one big Allow button, warnings, "left for you" and a hash prefix. Task timeline with a model badge per step. "What left my laptop today" and the zones editor. A simulated WhatsApp phone for rehearsal. Costs. A sandboxed widget host.
  - **Controls and security:** global STOP hotkey Ctrl/Cmd+Shift+X. Locked-down renderers (context isolation, sandbox, CSP, no navigation or permissions).
  - **Mock and tests:** pure logic modules tested with `node --test`, run from pytest. `npm run mock` plays the demo with no backend.
  - **Server fixes:** real uvicorn now serves websockets (`websockets` added), and the events handler no longer leaks on disconnect.
  - **Avatar art:** per the user, the avatar art is Muse's (M11); the app shows only a faceless glow until it exists.
  - 188 tests passing.
- M11: Avatar made by Muse. `lantern/avatar.py` and `scripts/make_avatar.py` render the canonical Lantern portrait with Muse Image, then 6 more expressions, each chained from the canonical turn. `--canonical` starts from the user's MetaMuse art, `--only` redoes some expressions on the saved anchor, and `--dry-run` shows the plan. Calls are audited and cost $0.01/image, logged to memory. `scripts/import_avatar.py` imports MetaMuse / Vibes exports, turning videos into VP9 loops with ffmpeg. The desktop reads `assets/avatar/manifest.js`, shows the art as a round portrait (looping video supported) with the idle art as fallback, and puts it in the panel header. The hand-drawn character was removed at the user's request; a faceless glow stands in until Muse art exists. 8 new tests; 196 passing.
- M12: Environment map (`lantern/envmap.py`). Finds projects under the allowed folders. Reads declared dependencies from requirements*.txt, pyproject (PEP 621, optional deps, Poetry), uv/poetry locks, Pipfile, package.json and environment.yml. Reads installed packages from each venv's dist-info, and conda envs via the conda CLI. Results go in an mtime-keyed SQLite cache. New tools (appended): `find_project`, `project_info` (including declared-but-missing packages), `which_projects_use`, and `run_in_project` (allowlisted commands in the project's own env, after approval, secrets stripped from the environment, output untrusted, pip installs verified). CLI: `--projects`, `--which torch` (no model call). 13 new tests, including a real pytest run inside a stub venv; 209 passing.
- M13: Widgets (`lantern/widgets.py`, `docs/WIDGETS.md`). `make_widget` tool (appended): Muse Spark writes one self-contained HTML document, a static checker rejects network, code-loading, host-access and obfuscation tricks (one retry with findings), and saving needs approval bound to the SHA-256 and is verified on disk. `GET /widgets` and `/widgets/{name}` re-check on every load. Pre-made `widgets/water-timer.html`: a timer and a joke, no health advice. Browser test: inside the real `widget-host.html`, a sneaky widget's fetch, parent access and localStorage are all blocked while the bridge works. The desktop mock serves the water timer. 40 new tests; 249 passing.
- M14: MCP. `lantern/mcp_client.py`: servers from `~/.lantern/mcp.json` (stdio or URL) become deferred tools (`defer_loading: true`) plus `tool_search`, only when at least one exists, with one-dot names, cleaned schemas and sanitized descriptions. Calls need approval unless allowlisted read-only, and results are untrusted. `lantern/mcp_server.py` + `python -m lantern --mcp`: Lantern's read-only tools over stdio (policy-checked, scrubbed, audited); `submit_task` / `task_status` are forwarded to the running server so approvals still apply. Ready for Meta AI Connectors. 7 new tests, including a real stdio subprocess; 256 passing.
- M15: Encryption at rest and real forgetting. `lantern/crypto.py`: Fernet, with the key in the OS keychain or a mode-600 file. Memory and the audit log encrypt goals, summaries, step contents, fact values and payload previews, and migrate old plaintext rows. `secure_delete` + `VACUUM`. `forget_task`, `forget_fact`, and `forget_everything` (which also erases Lantern's own voice notes, screenshots, outbox, browser profile and form profile through `policy.erase_lantern_data`). CLI `--forget-task/--forget-fact/--forget-everything`, server `/forget/*`, desktop "Forget" on each task and "Forget everything". Tests read the raw database files and find none of the planted strings. 10 new tests; 265 passing.
- M16: SAM 3.1 camera check-in (`lantern/sam.py`, `POST /checkin`, desktop "📷 Check-in"). One snapshot on click, objects only (people/face/emotion concepts refused in code), nothing stored, streamed and decoded with Meta's `meta_sam_parser`, audited and costed ($0.0025). The avatar shows a one-line reaction. Tests feed Meta's real parser a synthetic stream with valid masks. 20 new tests; 285 passing.
- M17: Reliability, rehearsal mode and the README.
  - **Retries:** `lantern/reliability.py` retries only temporary failures (429, 5xx, timeouts, dropped connections) with backoff, jitter and `Retry-After` (capped at 30 s). 400/401/404 fail fast. A circuit breaker pauses calls after 3 failures in a row. `explain()` turns any error into a sentence the user can act on. The retrying client wraps the audited client, so every attempt is audited; the OpenAI SDK's own silent retries are off.
  - **Visible failures:** retries are `retry` events, and the avatar says "the network's wobbly". Failed tasks say why and get a **Try again** button (`POST /tasks/{id}/retry`). `LANTERN_TIMEOUT` sets the per-call timeout.
  - **Rehearsal mode** (`lantern/demo.py`): `python -m lantern --demo-run` plays the 3-minute story in the terminal; `python -m lantern --serve --demo` serves it to the desktop app. The beats:
    - a voice note on the simulated phone
    - the reimbursement form filled by computer use in real Chromium, Submit approved on the phone, verified
    - the receipt emailed and found in Sent
    - a private-file question answered on the local lane
    - the planted key in the notes redacted
    - "which projects use torch?" from the project map
    - the water timer written, checked, approved and saved
  - **How the rehearsal works:** it runs on a fresh copy of the new `demo/home/` (receipt, notes with a planted fake key, a private offer letter, four projects including a venv with torch), with its own zones, profile, memory and key file. Only the model is scripted (`DemoModel`), with zero tokens, and the audit destination reads `rehearsal://nothing-sent`. The panel header shows "rehearsal".
  - **Security fixes found by the rehearsal:**
    - (1) Tool results travel as JSON strings, so a secret at the start of a line followed a literal `\n` and slipped past the scrubber's word boundary. The scrubber now scrubs decoded JSON values; clean payloads stay byte-identical.
    - (2) The scrubber could "redact" a Luhn-valid digit run inside a `call_id`, which would break the function-call chain. Wire ids (`call_id`, `id`, `type`, `role`, `status`) of input items now pass untouched; nested content is still scrubbed.
    - (3) Screenshot and voice-note filenames used dash-separated timestamps that could pass Luhn and get mangled in payloads; they now use underscores.
  - **README** rewritten: pitch, 5-command setup, the demo script, a mermaid architecture diagram, the Meta model map, the safety model, reliability, the WhatsApp scope note, cost so far and what to set up.
  - 24 new tests; 309 passing.

## Oct 26 (session 3: extras after M17)
- M18: Reminders (in the spec's WhatsApp scope, not built until now).
  - **Tools** (appended): `set_reminder`, `list_reminders` and `cancel_reminder`.
  - **Times:** `lantern/reminders.py` parses ISO times, "in 20 minutes" and "18:30"/"6pm" in code, refuses past times or anything over a year away, and repeats daily or weekly at the same wall-clock time.
  - **Storage:** a `reminders` table in memory, text encrypted, removed by forget-task and forget-everything. Upcoming reminders are in the model's briefing.
  - **Approval:** needed only after untrusted content has entered the task, so a web page can't schedule messages to your phone.
  - **Delivery:** a scheduler thread in Core delivers due reminders as `reminder` events. The hub sends "⏰ Reminder: …" to every phone channel, and late ones say so. `status` lists upcoming reminders.
  - **Panel and API:** a Reminders card with Cancel, a toast and the avatar's bubble; `GET /reminders` and `POST /reminders/{id}/cancel`.
  - **Mocks:** the desktop mock and the rehearsal model support it.
  - 16 new tests plus 2 desktop tests; 325 passing.
- M19: Tidy a folder.
  - **`organize_files`** (appended): up to 200 file moves (new subfolders as needed) under **one** approval. The card groups the plan by destination folder. The approval is bound to every file's path, size and modified time and re-checked right before acting, so a file swapped after "yes" stops the whole thing. Each move is verified on disk (destination present, source gone, same size). On the first failure Lantern stops and reports. The result carries an undo plan.
  - **Security fix:** moving a writable folder that contains a private or hidden sub-zone would have carried those files out of their zone (zones are paths). `policy.move` now refuses it, and `move_file` refuses before asking.
  - **Rehearsal:** the demo home gained a messy `Downloads/` (tiny valid JPG/PNG/PDF files), and "tidy my Downloads" is a rehearsal beat.
  - 13 new tests; 338 passing.
- M20: Resume after restart.
  - **On startup:** tasks still `active` from a previous run (crash, restart, closed laptop) are marked `interrupted` before the worker starts. The phone is told ("I was stopped in the middle of 1 task … reply resume"), and the panel shows a banner with **Resume all** and a **Resume** button per task.
  - **Ways to resume:** `resume` on WhatsApp/Telegram, `POST /tasks/resume`, Try again on an interrupted task, or `python -m lantern --resume`. The CLI forwards to a running server, or runs the tasks itself.
  - **What the resumed task knows:** the original goal plus what the cut-short run already did ("fill_form (verified)"), so it doesn't submit twice. Every change still needs approval anyway. The old task becomes `resumed`.
  - **Second server refused:** a second `--serve` on a busy port now exits with a clear message instead of marking the live server's tasks interrupted and then failing to bind.
  - 6 new tests plus 1 desktop test; 344 passing.
- M21: Setup doctor.
  - **What it checks:** `python -m lantern --doctor` (also `GET /doctor` and the panel's new **Setup** tab) covers Python, packages, Chromium, the API key (present/missing, never shown), `.env` not tracked by git, the computer-use model and calibration, the private lane (probed only when local), ffmpeg, WhatsApp variables and owner format, cloudflared, Telegram, SMTP, folders and zones (warns when nothing is private), encryption key store, Node + Electron, and the Muse face.
  - **How it reports:** each problem comes with its exact fix. A summary says whether the rehearsal and the real demo are ready, what blocks them and what's still to do.
  - **Read-only:** no keys created, no Meta calls.
  - 4 new tests plus 1 desktop test; 348 passing.
- M22: Packaging and CI.
  - **Packaging:** `pyproject.toml` makes Lantern an installable package with a `lantern` command (`pip install -e .`; `pip install -e ".[dev]"` adds pytest and pyflakes). The version comes from `lantern/__init__.py`.
  - **CI:** `.github/workflows/ci.yml` runs on each pull request and on pushes to main: pyflakes, the whole test suite (real Chromium, real uvicorn, the desktop's node tests), the full offline rehearsal (`lantern --demo-run`, which fails the build if any beat fails) and the doctor.
  - **Lint:** fixed the two unused imports pyflakes found.
  - 348 passing.
- End-to-end check of the real desktop app (asked: "are you sure this is complete end to end?").
  - **What it does:** `desktop/e2e/run.js` (`npm run e2e`, also run by pytest when Electron is installed) starts the real Python server in rehearsal mode and launches the real Electron 44.5.1 app (under Xvfb here). It clicks through the whole story in the panel: the form with two Allow clicks and verification, the private question, the planted key, projects, the widget with Allow and the sandboxed frame, a reminder, tidy Downloads with Allow, the phone, the Setup tab, costs and STOP. Screenshots are saved.
  - **Bug it found:** the email's Message-ID (from `make_msgid()`) carried a random run of up to 20 digits. About one time in ten it passed Luhn and showed up as a fake "card number" redaction in the privacy view. Lantern now makes its own letters-only Message-ID.
  - **Polish from the screenshots:**
    - The tidy approval card (desktop and phone) shows `home/Downloads/Images` instead of `/tmp/…` paths.
    - The zones list folds away built-in hidden paths that don't exist on this computer.
    - In rehearsal, the privacy view says "would have left this laptop (rehearsal: nothing was sent)".
  - `node_modules/` is gitignored; `desktop/package-lock.json` is committed.
  - 351 passing (including the Electron e2e) and 18 desktop tests.

## Oct 26–27 (design pass)
- M23: Apple-style design system and panel redesign.
  - **Design tokens:** system colours, grouped background and separators for light *and* dark, the system font stack (nothing bundled), translucent toolbar and tab bar (backdrop blur), spring-like motion that respects "reduce motion", focus rings, and a high-contrast tweak.
  - **Panel:**
    - A bottom tab bar with our own line icons (Ctrl/Cmd+1…6 switch tabs).
    - Large titles and inset grouped lists.
    - A **Today** view with a greeting, a search-style ask field, one-tap suggestions and four at-a-glance tiles (needs you, done today, next reminder, spent today).
    - Task cards with a status ring and collapsible steps.
    - An approval *sheet* with Deny / Allow (the default action on the right) and form fields shown as a two-column table. It falls back to the raw text whenever parsing isn't exact, so nothing approved is hidden.
    - The privacy hero is a big number with a caption, and zone rows show the folder name over the path.
    - A cost meter against the $150 budget.
  - **Bugs found by reviewing screenshots:** nested children rendered as `[object HTMLElement]`, and the status line lost its class. The e2e now fails on any `[object …]`, `undefined` or `NaN` in the panel, and checks dark mode.
  - 2 new desktop tests; 351 passing.
- M24: Live activity island.
  - **What it shows:** above Lantern's face, a black capsule says what Lantern is doing right now: "Clicking at (412, 315) · Wonder", "Network wobbly · retrying", "Done · verified", a reminder. It grows into a small card for approvals, results and reminders (spring animation, reduced-motion aware). `lib/activity.js` is pure and tested.
  - **Approving from the island:** Deny / Allow, bound to the same nonce and payload hash as the panel. Quick **Allow** appears only for clean approvals; anything flagged (untrusted content read, warnings, expired) shows **Review…**, which opens the full sheet in the panel.
  - **Security fix found while building it:** form tasks were only marked tainted *after* `fill_form` returned, although the page's labels and screenshots had already reached the model. `fill_form` now taints the task before opening the page, so the Submit card is marked "after reading untrusted content" (test added).
  - The e2e approves the widget from the island and checks that the form submit offers Review only.
  - 1 new desktop test; 351 passing.
- M25: Quick Ask.
  - **What it is:** **Ctrl/Cmd+Shift+Space** anywhere opens a Spotlight-style bar in the upper middle of the screen. It uses native glass where the OS has it (macOS vibrancy, Windows 11 acrylic) and draws its own surface elsewhere.
  - **How it works:** type and press Return to start a task, then it hides and the island shows progress. ↑/↓ move through "Ask Lantern", suggestions, recent tasks and "N approvals waiting → open". "stop" runs the kill switch. Esc or clicking away closes it.
  - **Code and tests:** `lib/quick.js` (rows, prefix matching, wrap-around) is pure and tested. The window uses the same locked-down preferences and CSP as the others, and the static security test now covers `quick.html` and `quick.js`. The e2e opens Quick Ask, checks the rows, starts a task from it and checks that it finishes and the bar hides.
  - 1 new desktop test; 351 passing.
- M26: Native integration.
  - **Menu bar / tray icon:** our own lantern glyph, drawn by `scripts/make_tray_icons.py` (stdlib, anti-aliased): a black Template image on macOS, amber elsewhere. Its menu shows the live status ("Working: Clicking at…", "1 approval waiting", "Offline…"), Ask Lantern…, Review approvals / Open Lantern, Stop everything and Quit. On macOS the pending count sits next to the icon.
  - **Badge:** the app badge shows the number of pending approvals.
  - **System notifications:** approvals while the panel isn't in front (title only, never values), reminders, and failures. A click opens the panel.
  - **Code and tests:** the main process follows Lantern's events over its own WebSocket. `lib/native.js` is pure and tested. Mock mode now keeps the platform settings, and the avatar's right-click menu gained "Ask Lantern…". The e2e checks "1 approval waiting", the badge, that it clears, and that no notification carries form values; a test keeps the committed tray PNGs identical to what the script draws.
  - 1 new Python test plus 1 desktop test; 352 passing.
- Fix: the doctor and NEXT_SESSION told you to set `LANTERN_FORMS_PROTOCOL` / `LANTERN_FORMS_COORDS`, but `config.py` reads `LANTERN_CUA_PROTOCOL` / `LANTERN_CUA_COORDS`, so following the advice would silently have done nothing. Everything now uses the names config reads. `python scripts/smoke.py cua` ends with the exact `.env` line to paste, and a test checks that config picks that line up.
- M27: Welcome and Preferences.
  - **Welcome sheet:** on first launch, a native-style sheet slides up: what Lantern does, that it asks before anything changes, that private files stay on the computer, the Quick Ask shortcut. It has Continue and "Check this computer's setup".
  - **Preferences:** the Setup tab gained a personality segmented control (Smug / Sarcastic / Helpful), saved through `POST /settings` into `config.json` next to `memory.db` (only allowlisted keys; under `--demo` that's the rehearsal's own folder). The next task uses it. A shortcuts list shows the right modifier keys for the platform.
  - **e2e:** Electron app data can be isolated with `LANTERN_USER_DATA`. The e2e sees the welcome sheet, dismisses it, switches the personality and checks it was saved.
  - **Also:** the calibration variable names were fixed (see above).
  - 1 new Python test; 354 passing.
- M28: Submission kit. `docs/SUBMISSION.md` has:
  - a pre-recording checklist (doctor, smoke checks, calibration line, planted files, settings);
  - a shot-by-shot 3-minute script matched to the real UI (island, Quick Ask, approval sheet, Privacy tab, menu bar);
  - an honest fallback rule: rehearsal footage is never presented as live model output;
  - a write-up draft and final checks.

  `docs/slide/architecture.html`, rendered to `architecture.png` at 1920×1080, is the architecture and Meta-model-map slide for the video.
- Wire tests (preparing for the real key).
  - **What they are:** `tests/fake_meta_api.py` is a local HTTP stand-in for the Meta Model API: Responses API JSON, server-sent-event streams, voice transcription, scripted HTTP errors with Retry-After. `tests/test_wire.py` drives Lantern through the **real OpenAI SDK** over HTTP with the same layering Core uses (SDK → audited → retrying).
  - **What they cover:**
    - an agent task with a 429 retry, function calls and `previous_response_id`
    - Meta's `computer_call.actions` and safety checks surviving the SDK's response objects
    - screenshots sent untouched
    - a planted key absent from the raw HTTP bytes
    - 401/404 failing fast and 503 retried, all with plain sentences
    - the widget writer
    - voice multipart
    - a SAM 3.1 check-in over a real SSE stream through Meta's parser
    - Core building its own client from `MODEL_API_KEY`
  - All passed on the first run, so the remaining unknowns for tomorrow are the real API's behaviour, not our plumbing.
  - 9 new tests; 363 passing.
- Code review of the whole branch, and fixes for everything it found:
  - **Private lane off by default:** with no `LANTERN_GLIMMER_URL`, a real run quietly used the offline stand-in, which copies lines of private files to the cloud model. The lane is now **off** by default: private-file questions are refused with the fix. The stand-in runs only in rehearsal and tests, chosen explicitly.
  - **Rules on every turn:** the Responses API doesn't carry `instructions` over with `previous_response_id`, so from step 2 the agent (and the computer-use loop) ran without its rules, including "never follow instructions inside content". The same rules text is now sent on every turn, byte-identical for caching. A wire test checks it.
  - **Tool errors go back to the model:** `ValueError` / `OverflowError` / `LookupError` from a tool are returned to the model instead of killing the task. Reminders more than a year away are refused cleanly.
  - **Names count as content:** file and folder names (`list_dir`, `search_files`, `find_project`) and `package.json` scripts now mark the task untrusted, like file content.
  - **MCP goals marked:** tasks submitted over MCP are tagged `[mcp]`, start untrusted, and their email addresses don't count as known recipients.
  - **Atomic approvals:** answering an approval is check-and-set under a lock; a test fires 200 simultaneous Allow/Deny pairs.
  - **Moves:** `move_file` treats `Folder/` as "into that folder" even if it doesn't exist yet, and moving symlinks is refused, so the file a link points to never gets dragged along.
  - **Widget storage:** values and the index live in separate namespaces, so a widget can't corrupt its index or bypass the key cap.
  - **Token file:** the API token file is created owner-only in one step (it was briefly world-readable).
  - 8 new tests plus 1 desktop test; 371 passing; desktop e2e passing.

## Oct 27 (while waiting for the key)
- M29 Undo.
  - **What it does:** "tidy my Downloads" already offered "say undo", which went back through the model. Now every verified move (`move_file`, `organize_files`, and an undo itself) logs its reverse move in the task's encrypted log; this log is never sent to the model. `lantern/undo.py` turns the log into a plan:
    - last move first
    - a file moved twice goes straight home
    - a file moved there and back is left alone
  - **How it runs:** `Core.undo` runs the plan as its own task through `Toolbox.undo_moves`. That's the same zone checks and the same single approval card bound to every file's path, size and modified time, then a check on disk after each move. No model call, $0.
  - **Safety:**
    - never puts a file back over a new one with the same name (refused before asking)
    - skips files the user already put back by hand
    - respects zones changed since
    - folders are put back too
    - empty folders the tidy created stay, since Lantern never deletes
  - **Where you use it:**
    - desktop: an **Undo** button on task cards, then "Undone in #N" on the card
    - phone: `undo` / `undo that` (multi-level: each UNDO goes one task further back)
    - typing "undo" in the panel or Quick Ask
    - `POST /tasks/{id}/undo`; `/tasks` reports `undoable`
  - **Edge cases:**
    - a declined undo can be tried again (as an undo, never through the model), and an interrupted one resumes as an undo
    - a partial undo (a move failed halfway) isn't used up: the next try puts back the rest
    - a double tap queues one undo
    - typed "undo" waits while another task is running
    - `undo_moves` is not a model tool
    - MCP goals saying "undo" go to the model as text, never as the shortcut
  - **Tests:** 12 new tests plus 1 desktop test; the Electron e2e now taps Undo after the rehearsal tidy and checks all 6 files are back on disk. 383 passing.
- M30 Spending limits.
  - **Why:** CLAUDE.md sets a $150 budget, but nothing enforced it. A computer-use loop on Muse Wonder could have eaten the credit on day one.
  - **How it's enforced:** `lantern/budget.py` checks three limits before every model call, as a `before_call` hook in `RetryingClient`. The limits are total (`LANTERN_BUDGET_USD`, default 150), per task (`LANTERN_TASK_BUDGET_USD`, default 3) and daily (`LANTERN_DAILY_BUDGET_USD`, off by default). Both Core and the CLI agent use it.
  - **What a refusal does:**
    - the refused call never reaches the wire, is never retried, and doesn't trip the circuit breaker
    - the task fails with a sentence naming the limit and the variable to raise
    - one warning event at 80% of the total
  - **Typos and the ledger:**
    - a typo in a value keeps the default (never "off"), and `--doctor` flags it
    - spending is counted in a new `spend` table (dollars per local day, no task links, no content), so forgetting tasks or everything doesn't lower it
    - older databases seed it from their usage rows
  - **Where you see it:**
    - `/usage` returns the limits
    - the Costs tab shows "$X of $150 spent", a meter that turns red past 80%, and "Stops itself at $150.00 · $3.00 per task."
    - the Today tile shows what's left
    - `--doctor` has a "Spending limits" row
    - `.env.example` lists the variables
  - **Tests:** 7 new tests plus 1 desktop test and 1 e2e check; 390 passing.
- Windows pass (the demo laptop runs Windows; everything so far was tested on Linux).
  - **The bug:** 27 places read or wrote text with the OS default encoding, which is cp1252 on Windows. Among them:
    - the policy's own `write_text` (files the model writes)
    - widget HTML
    - the form vault
    - settings, zones and the avatar manifest
    - subprocess output from `run_in_project` and the doctor
  - **What it would have done:** a ₹, → or emoji would have raised `UnicodeEncodeError` mid-task, and UTF-8 files would have read back garbled.
  - **The fix:**
    - every site now says `encoding="utf-8"`
    - the policy reads and writes with `newline=""`, so a file read back is byte-identical to what was written and `verified` means the same on every OS (content containing `\r\n` used to fail verification even on Linux)
    - CLI and script prints use `errors="replace"`, so a piped cp1252 console can't crash on ✓ or 🏮
  - **Keeping it fixed:** `tests/test_portability.py` parses every module and fails on any text-mode open, `read_text`, `write_text` or text subprocess without an explicit encoding. Run against the previous commit, it finds all 27. Zone matching was already safe: it compares `Path` objects, and `WindowsPath` is case-insensitive.
  - 3 new tests; 393 passing.
- Desktop on Windows:
  - **AppUserModelID:** set on win32. Unpackaged, Electron's path is the ID; packaged, `app.lantern.desktop`. Without it Windows may drop the approval and reminder notifications.
  - **One instance:** a single-instance lock, so a second launch hands over to the running app (shows the panel) and exits instead of adding a second tray icon and losing the global shortcuts. The lock follows `LANTERN_USER_DATA`, so tests and rehearsals stay isolated.
  - **Checks:** the e2e now launches a second copy and checks it exits; a static test checks the setup order. 394 passing.
- Windows bring-up:
  - **`.env` encodings:** Windows PowerShell 5.1's `echo MODEL_API_KEY=... > .env` saves UTF-16 with a BOM, and python-dotenv reads UTF-8, so Lantern would have crashed at startup or lost the key. `config.load_env()` now detects UTF-16 / UTF-8-BOM / UTF-8 and is used by the CLI, the server and `scripts/smoke.py`.
  - **README:** a PowerShell quick start (PowerShell 5.1 has no `&&`; `py -m lantern` works without pip's Scripts folder on PATH).
  - 1 new test; 395 passing.
- Accessibility pass.
  - **The check:** the Electron e2e now runs axe-core (WCAG 2.1 A/AA) on:
    - all six panel tabs, in light and dark
    - Quick Ask
    - the island while it asks for approval, and the avatar window
  - Any violation fails the run. The axe script is injected through the debugger protocol, so the pages' strict CSP stays as shipped.
  - **First run:** 14 kinds of findings.
    - the privacy-zone selects had no accessible name (now "Zone for <path>")
    - Apple's system colours used as small text fell short of 4.5:1: secondary labels 3.6:1, the blue tint 4.0:1, orange 2.2:1, green 2.2:1, unselected tab labels 3.1:1, and white on the red Stop button 3.5:1
  - **Fix:** text and fill tokens are split.
    - fills and dots keep the system colours
    - text uses `--tint-text` / `--red-text` / `--orange-text` / `--green-text` (lighter variants in dark mode) and a darker `--label-2`
    - white-text buttons use `--tint-fill` / `--red-fill`
    - model chips mix their hue toward the label colour, so they pass in both modes
    - Quick Ask and the island got the same treatment
  - **Result:** 0 violations. The README hero and gallery were re-captured from this run.
- One command for the desktop.
  - **What changed:** when nothing answers `/health`, the desktop starts the server itself before opening its windows, so a first run's token already exists.
    - command: `py -m lantern --serve` on Windows, `python3` elsewhere, with `LANTERN_PYTHON` to override, on the port from `LANTERN_API`
    - `npm run rehearsal` adds `--demo`
    - the server's output goes to `server.log` in the app's data folder
    - quitting stops only a server the app started
    - a server you started yourself is used as is; `LANTERN_NO_AUTOSTART=1` opts out
  - **Checks:** the e2e launches the app with no server running and checks three things: the panel reaches the server it started, quitting stops that server, and the log was kept.
  - **Docs:** the README, desktop README and submission checklist now say `npm start` / `npm run rehearsal`.
- M31 Task receipts.
  - **What it shows:** `lantern/receipt.py` builds a task's receipt from the task's steps, approval outcomes, the audit rows for the task and its usage:
    - each change with whether it was checked on disk or via the API
    - lookups counted, not itemised
    - each approval: what it asked, approved or denied, where (desktop, WhatsApp, the simulated phone), used once, the payload hash
    - every destination with bytes, call count and kind (model / email / message / voice / MCP)
    - what was redacted, private-lane questions that stayed local, and cost per model
  - **Honest about stand-ins:** rehearsal and fake modes say "Would have sent … Simulated: nothing actually left" instead of claiming data left.
  - **Where you see it:**
    - the desktop: a **Receipt** button on every finished task opens a sheet (Escape closes it; passes axe)
    - the phone: "receipt" / "receipt 12"
    - the CLI: `--receipt 12`
    - the API: `GET /tasks/{id}/receipt`, with a `text` version
  - **What changed underneath:**
    - Core now logs approval outcomes (decided / expired / used) as encrypted task steps.
    - **Fix found on the way:** the private lane's audit rows weren't tagged with their task, so "forget this task" left them behind. Each row keeps a 300-character preview of the question and document, encrypted. The router now tags them (`Router.set_task`, called from `Toolbox.begin_task`), so they show on the receipt and go with the task.
  - **Tests:** 5 new tests, 1 desktop test, e2e checks on the main form task's receipt.

## Oct 27 (live API testing, session 5)
- First real calls: all five hosted models pass `scripts/smoke.py` (about $0.016). Computer use calibrated: Spark takes the native tool (pixels), Wonder only the function harness (0..1000); `LANTERN_CUA_PROTOCOL=auto` now picks per model.
- `scripts/live_story.py`: plays the demo story plus safety beats (prompt injection, hidden folders, tidy + undo, receipt) against the live API on a fresh rehearsal folder, records every request after scrubbing and checks that the planted key and the private salary never leave. The voice beat is real speech (espeak-ng → OGG/Opus → ffmpeg → Muse Voice Transcribe).
- Fixed from live runs: private-lane answers were re-sent in later briefings; `.wav` voice notes were converted onto themselves.
- Also verified live: Muse Image avatar sheet (7 consistent expressions, $0.07), SAM 3.1 (5 ducks on the cookbook photo, 0 mugs), tool search with a deferred MCP tool catalog (model searched, loaded and called the right tool).
- Also verified live: the Electron desktop app against a live server (`npm run live`: torch question + tidy with Allow), resume after killing the server mid-task, a $0.002 per-task limit stopping a task after one call, the private lane OFF refusing without leaking, and a re-run of the widget, tidy + undo, prompt-injection, hidden-folder and recap beats. Total live spend for all testing: about $0.90.
- Fixed: widget generation streamed at `reasoning.effort: low` (the unstreamed call always died at 30 s); no health guidance (agent rule, widget rule, checker catches intake goals like "8 glasses"); tiny budget limits printed exactly.

## Oct 27 (Phase 2, S1: scheduler, branch claude/phase2-core)
- `lantern/scheduler.py`: N run slots (default 3), a thread per task, FIFO slots with a hand-off after every model turn (round-robin). Tasks park while they wait: waiting_user, waiting_approval, backoff, waiting_browser. Each task has its own toolbox, audited client, spending check and approval gate.
- `lantern/taskrun.py`: `ResumableAgent`, the agent loop with a checkpoint at every step (`memory.task_state`, encrypted). `ask_user` parks the task and its thread ends; the answer finishes the same turn on the same response chain. After a restart, every unfinished task continues by itself; in-flight sends, submits and moves are never re-run blindly (`recovery.py`).
- New tool `ask_user(question, options)` (last in the tool list; questions fishing for secrets are refused in code). New events: `task_parked`, `task_resumed`, `questions`, `juggling`. New routes: `POST /tasks/{id}/answer`, `GET /questions`, `GET /scheduler`. On the phone: "#12 answer" or an option button; several questions at once go out as one message. The desktop shows question cards, parked tasks and a juggling avatar.
- Small hooks in shared files: `Core(workers=…)`, which defaults to 0, the old one-at-a-time mode; `recover_interrupted(keep=…)`; `Agent._failed`; an `AuditLog` lock; the `task_state` / `ladder_stats` tables.
- Real target: `tests/test_scheduler_portal.py` sends three tasks from the phone at once. The permit task asks which permit type and parks; "email the slides" and "tidy Downloads" finish meanwhile. The button answer arrives, and Lantern fills all six pages of `demo/portal` in headless Chromium and gets a reference number. The test checks the portal's own record, including the uploaded file's SHA-256.
- That run found a form-filler bug: on pages with Back and Next, it clicked Back. Fixed.
- Tests: 16 scheduler tests + 2 portal tests (scripted FakeClient models; nothing calls the real API) + 3 desktop tests.
- Real-API check: not run in this session. This container has no MODEL_API_KEY and its network policy refuses api.meta.ai. `scripts/phase2_live.py` (wrap-up) does it where the key works.

## Oct 27 (Phase 2, S2: resilience engine)
- `lantern/recovery.py`: every tool call passes through `Recovery.before_call` / `after_call`, one `Recovery` per task, rebuilt from the task's steps after a pause or restart. The six failure classes and what each does:
  - **transient:** parked backoff, then 2 longer parked waits.
  - **tool_error:** the error is fed back; the same failing call twice in a row is refused.
  - **approach_failed:** the next approach for that step.
  - **model_confused:** bad arguments twice, the same call 3 times, or no progress in 6 turns. Escalates via the ladder (S3).
  - **needs_human:** steered to `ask_user`, which parks the task.
  - **impossible:** a step failed 3 times, every rung was tried, or a spending limit was hit. The task stops with a report.
- Every decision is logged as an `attempt` step (class, strategy, rung, cost, result) and shown on the receipt and as a `recovery` event.
- Never twice: a send, submit or move that happened, or may have happened, is refused for the rest of the task. It is keyed on its effect (form URL, recipients + subject, source path), unless files on disk prove a move didn't happen.
- Tests: 14 in `tests/test_recovery.py`:
  - each failure class, scripted;
  - the same email never sent twice, including across an `ask_user` pause and across a restart that cut a send off (the new process sends nothing);
  - a move that provably didn't happen may be retried.
- Real-API check: not possible in this container (no key, api.meta.ai blocked); see `scripts/phase2_live.py`.

## Oct 27 (Phase 2, S3: model ladder and learning router)
- `lantern/ladder.py`: rungs 0 routine, 1 rules, 2 Glimmer (local), 3 Spark low, 4 Spark high, 5 Wonder. Step kinds: plan, reply, file_op, lookup, extract, classify, form_fill, gui_action, generate. How a rung is picked:
  - the lowest rung with ≥ 80% recent success (optimistic start of 4 imaginary successes), within the task's budget;
  - one rung up on failure for the rest of the task;
  - the rung below gets another try after 5 straight successes;
  - private content capped at rung 2.
- In the scheduler: every agent turn runs on its rung (model plus `reasoning.effort`). Moving to Wonder starts a fresh chain with a recap. Recovery's model_confused moves the planner up; a fill that didn't verify moves computer use from Spark to Wonder. Outcomes go to `memory.ladder_stats`.
- Every usage and tool event carries `rung` and `rung_badge` ("Rule", "Muse Glimmer (local)", "Muse Spark · low", "Muse Wonder"), and the desktop shows them on each step. `lantern --router` and `GET /router` print kind × rung → success %, average cost, average time.
- Tests: 10 in `tests/test_ladder.py`:
  - the rule (escalate, de-escalate, budget);
  - a failed fill moves computer use to Wonder;
  - rungs on every event;
  - a private question stays on rung 2 when the local model is down, and the file never reaches a cloud payload;
  - the CLI and API table.
  - Plus the escalation path in `tests/test_recovery.py` (Spark low → Spark high → Wonder with a recap) and 1 desktop test.
- Real-API check: not possible in this container (no key, api.meta.ai blocked); see `scripts/phase2_live.py`.

## Oct 27 (Phase 2 wrap-up)
- `scripts/phase2_live.py`: S1–S3 on the live API, in 5 checks, in a fresh rehearsal folder each:
  1. three tasks at once, with one parked for an answer;
  2. a tool error, then another way;
  3. never the same email twice;
  4. Spark high, then Wonder on a fresh chain;
  5. shutdown mid-task, and a new process continues.
  - It prints measured cost and the ladder table per check, with a $0.25 per-task limit and `--max-usd` (default $1).
  - `tests/test_phase2_live_script.py` dry-runs all 5 against a scripted model (plumbing only). **The real run is still to do**; this container has no key and can't reach api.meta.ai.
- `demo.build(workers=…)` so the rehearsal folder can run the scheduler.
- `docs/NEXT_SESSION.md`: the Phase 2 state, the live check to run, known limits, and what Session B builds next (S4, S0, S8).

## Oct 27 (Phase 2, S4: self-compiling routines)
- `lantern/compiler.py` + `lantern/routines/` (contract, checker, runtime, store, runner):
  - **learn:** a verified task becomes a Routine Contract (from the local traces; vault placeholders, enum params, postconditions, a success check). Muse Spark names and describes it and may add params, but can't change the plan. Muse Spark then writes `routine.py`, which must pass the whitelist checker and make exactly the contract's calls, or Lantern writes it itself.
  - **verify:** on a sandbox copy of the site, or dry up to the first submit. Only then is it trusted. Stored in `~/.lantern/routines/<name>/v<N>/`.
  - **match:** by the goal's words, enum values included (no model), or one cheap call when unsure.
  - **run (rung 0):** one plan-bound approval, zero model calls, every target cross-checked in the page, every step checked.
  - **heal:** a moved field or button gets one Muse Spark call to pick the new target from the page; the run passes, then v<N+1> is verified and saved.
  - Learning is offered after a verified task, and happens automatically for a repeated goal.
- Forms: session cookies carried per task between `fill_form` calls (multi-page portals now work with a fresh browser per page); `advanced` for pages that move on; local `trace` steps; stable option values.
- Everywhere: "learn [N]" on the phone, `POST /routines/learn`, `GET /routines`, `POST /routines/{name}/forget`, `lantern --routines`, a routine line on receipts, routines erased by "forget everything". On the desktop: a learn offer card, and Learned routines in Costs with a cost-per-run chart and heal marks.
- Tests:
  - **The full story on `demo/portal` in Chromium** (`tests/test_routines.py`): the agent fills 6 pages; learning costs 2 scripted Spark calls and is verified on a fresh portal; the rerun takes 0 model calls and 1 approval; then the layout flips to v2 and every label, id and button changes; the run heals (one repair call per moved target) and saves v2, verified on a fresh v2 portal; the next run is back at 0 model calls.
  - **Unit tests:** checker (14 forbidden shapes), runtime (dry stop, plan-bound approval, hard stops, heal by re-targeting), store math, matching, fallback, restart, API.
  - Plus `scripts/phase2_live.py` check 6, dry-run on a scripted model.
- Guards that caught things on the way: the renderer lockdown test (no `innerHTML`: the chart is built with DOM calls), the "only policy.py touches files" test (a routine is forgotten through `erase_lantern_data`), and the matcher test (a model could have set `base_url`; now urls, paths and secrets are never a model's pick).
- Real-API check: not possible in this container (no key, api.meta.ai blocked). `python scripts/phase2_live.py --only 6` runs it where the key works.

## Oct 27 (Phase 2, S0: security backbone)
- `lantern/labels.py` (labels that join, per-task handles), `lantern/reader.py` (the tool-less Reader with typed answers), `lantern/security.py` (the scheduler's guarded toolbox, risk tiers, target checks):
  - **Planner/Reader split.** Content from files, pages, emails, commands and other programs reaches the planner only as `$doc` handles; it learns from them through `ask_reader` (new tool, added at the end of the list). Text answers are handles too.
  - **Labels.** Recipients, links, commands and tool inputs that came from content are flagged on red cards; a form at such an address, or a read-only tool with such input, asks first.
  - **Private answers** (`ask_private`) are handles that stay on this computer: filled in on the user's screen, a placeholder on the phone, refused by memory, and refused by the audited client if a cloud request would carry one.
  - **Tiers** green/yellow/red on every tool and card; red is never one tap on the desktop.
- Scrubber: env-style secret names and one-time codes. Reader + code check for instruction-shaped text.
- `LANTERN_SCREENSHOTS=masked`: black boxes over the user's own values before a screenshot goes to the computer-use model (counted in raw mode).
- `scripts/eval_privacy.py`: raw vs masked on the clean and attack portal; success, steps, cost, screenshot exposure, leaks of private values or planted secrets in any payload that left, payloads matched to the audit log by hash. Dry-run checked here (4 runs, 13/13 payloads per run in the audit log); **the real run is still to do** (no key in this container).
- Tests: `tests/test_security.py` (17), `tests/test_injection.py` (13: attacks A1-A7 with honest, compromised-planner and compromised-reader scripts, the portal in attack mode in Chromium), `tests/test_eval_privacy.py` (5: masking checked pixel by pixel in Chromium, the eval dry run). Found and fixed on the way: handle names were filled in before `ask_reader` saw them, and the resulting error quoted the document back to the planner; MCP tools are called by their own names, so their inputs weren't checked; the local reader path lost the instructions flag; the scrubber missed `LANTERN_DEMO_API_KEY=` and `last OTP:`.

## Oct 27 (Phase 2, S8: teach once by demonstration)
- `lantern/recorder.py`:
  - Records a person doing a web task once in Lantern's browser. It sees page fields at load and at each button press, plus each change; a password field reports only that something was typed.
  - Turns the recording into the same `trace` steps `fill_form` writes, so S4 learns, verifies and stores it unchanged. Narration notes go to the contract call; no screenshots are sent.
- `Core.teach` / `teach_stop` / `teach_note`; `POST /teach`, `/teach/stop`, `/teach/note`; `lantern --teach URL TASK`. In the desktop: Costs → *Show Lantern once*, and a "Watching you" card with Done and notes; teach events carry counts, never values. STOP throws a recording away.
- Forms driver: `guard_submits=False` for the person's own browser; `record()` and `confirmation()`.
- Tests: `tests/test_teach.py` (6). The headline test: a simulated person fills all 6 pages of `demo/portal` in Chromium while Lantern records. The recording is learned (one scripted contract call, which receives the narration, vault placeholders and no screenshots), verified on a fresh portal, and then the same goal runs with 0 model calls and 1 approval. The other tests cover going back and resending, password values never kept, private files never picked, no confirmation means nothing is learned, STOP keeps nothing, and the routes. `desktop/test/teach.test.js` (3).
- Found on the way: Lantern's submit guard blocked the person's own submits in the teaching browser; a page's first load report can arrive before the recorder listens (pages now start from their first event).

## Oct 27 (live QA, session 5 continued; branch claude/live-api-qa)
- Tested live, every way the sandbox allows: forms under attack (traps page with password/OTP/card/CVC/Aadhaar/PAN/UPI PIN/CAPTCHA on Spark and Wonder: only the email filled), deny paths, a real external site (httpbin, filled, Submit denied), voice (names, Hindi, Hinglish, a 47 s note, silence, 8 kHz), phone STOP/status/receipt/undo/queueing, the sent email's contents, SAM edge cases (3000 px photo, injected concepts), Muse Image redo and own-art paths, import of video art, reasoning effort and prompt caching.
- Fixed: plain-words secrets ("my password is…", OTP, CVC, UPI PIN) reached the cloud; Hindi/Hinglish voice notes needed `languageBias` (`LANTERN_VOICE_LANGUAGES`); declined Submit was reported as "not filled"; labels like "Name:" read "Name::"; a lost response chain failed the task (now continues from a recap); the phone said "On it" for a queued task; the memory briefing broke prompt caching (now in the input, plus `prompt_cache_key`: 90-94% cached).

## Oct 28 (Phase 2, S5 extras: a changed flow is re-learned)
- `demo/portal` flow **b** (`?flow=b`, `--flow b`): a new required Contact page between Address and Permit (a process change, not a redesign).
- `compiler.before_final_submit`: a routine that stops before its last submit falls back to the agent (earlier Next presses only moved the form along). When the agent's run verifies, it is re-learned as the routine's next version (`Core.learn(replace=…)`), verified on a fresh copy of the changed site.
- Tests: `tests/test_relearn.py` (2). The main one learns the portal on flow a, then the portal switches to flow b. The routine stops before submitting anything, the agent files the 7-page form, and that run is re-learned as v2 and verified on a fresh flow-b portal. The next run takes 0 model calls and 1 approval.
- Also S5: **file tasks become routines.** A verified tidy-up compiles into rules by pattern (`files.sort(folder, match, to)`: `*.jpg → Images`, by exact name when one extension went two ways), with the folder as a param.
  - The run asks once, with the exact moves listed. It moves only those; a file that arrived after the card stays put, and nothing is overwritten. Moves are checked on disk and undoable.
  - Tests: `tests/test_file_routines.py` (3). The agent tidies Downloads once; the run is learned; a new week's files are sorted by the rules with 0 model calls and one card; a late arrival stays; Undo puts it all back.

## Oct 28 (Phase 2, S10 photo to scan, S9 glasses → laptop handoff)
- `lantern/scan.py` (S10): SAM 3.1 finds the page (document/paper/letter, largest mask). The corners come from the mask, then Lantern straightens the page (Pillow perspective), cleans it (greyscale, autocontrast) and saves PNG + PDF through policy.py in a writable `Scans` folder. Without a page, or with SAM off, it uses the whole photo and says so.
- `lantern/handoff.py` + `Core.photo` (S9):
  - A photo on WhatsApp, Telegram or the fake phone is scanned, then read by Muse Spark (no tools, JSON checked in code), and the files are renamed by what the paper is.
  - The caption, or the voice note or message sent right after it, picks the learned routine; the routine runs with the scan attached after the approval on the phone, and the result comes back as one line.
  - Without words the paper's kind is tried, with a warning on the card and no values from the paper.
- Routines: file uploads are `path` params; the approval card lists attachments first; a routine's summary counts only its own model calls.
- Tests: `tests/test_scan.py` (4). A synthetic tilted page and its SAM mask, through Meta's real parser: corners within 4 px, the page straightened to its own size, no desk left in the corners, PNG and PDF via policy.py, no overwrite, the fallbacks.
- `tests/test_handoff.py` (5): a photo with words on the fake phone runs the learned portal routine in Chromium; the portal's stored file has the scan's SHA-256. A photo without words waits, then the paper's kind chooses, with a flagged card and no paper-set values. Also the words after a photo, the reading's shape, and WhatsApp and Telegram photos.

## Oct 28 (Phase 2, S11: Sleep)
- `lantern/sleep.py` + `Core.sleep`:
  - **Each night** (03:30 with `lantern --serve`; `lantern --sleep`; `POST /sleep`) every trusted routine is replayed where nothing real can happen: a sandbox copy of the site, or dry up to the first submit or move. Drift is repaired (S4) and verified as a new version.
  - **Suggestions:** repeated unlearned goals and routines that do the same steps.
  - **A morning digest** goes to the phone and the desktop.
- Tests: `tests/test_sleep.py` (3):
  - the portal redesigned overnight (v2) is healed on a sandbox copy, the real portal gets nothing, the digest reaches the phone, and the morning run takes 0 model calls with no healing;
  - a routine whose repair fails "needs you";
  - suggestions and the schedule.

## Oct 28 (Phase 2, S12: "Do this Reel")
- `lantern/reel.py` + `Core.reel`: a shared how-to video becomes ffmpeg frames plus Muse Voice Transcribe speech, then Muse Spark steps (images + transcript, structured output, no tools). A review in code refuses private and hidden folders, marks security and install steps, and stops at an unclear step. The user approves the exact list on one red card; the normal agent does it; when it worked, Lantern offers to learn it.
- Video intake on WhatsApp, Telegram and the fake phone; `POST /reel {path}`; a desktop card title.
- Tests: `tests/test_reel.py` (5) with a real ffmpeg-made video. A tidy-up video becomes 2 frames plus a transcript, then 2 steps; one card; the agent moves the photos; a learn offer follows. Also: an unclear step asks and nothing happens; the review rules; frames and the structured-output fallback; the phone intake.

## Oct 28 (Phase 2, S6: Lantern Network)
- `lantern/network.py` + Core: Ed25519 identities and a contact allowlist, plus signed, fresh, single-use messages over the channel layer (`send_to` on WhatsApp, Telegram and fake; the hub routes `🏮LN1` messages).
  - **Requests:** ask for a file (one small text file, scrubbed, never from private or hidden zones) and share a routine (imported, trusted only after it verifies).
  - **Rules:** the owner's words become a Cedar policy via Muse Spark, with examples decided by cedarpy; every allowed request still needs the owner's approval.
  - **Receipts** on both sides; `GET /network`, `POST /network/contacts`, `/network/rules`, `/network/ask`. cedarpy is an optional extra (`network`).
- Tests: `tests/test_network.py` (6). Two Lanterns in one process: a bill shared with the owner's approval, scrubbed; a private file never found; a folder outside the rules refused with no card; a routine refused by the rules, then imported but not trusted until it has its own folder. Every bad message is refused (forged, stranger, replayed, stale, misaddressed, bad fingerprint). Rules compile with the engine's own decisions. The hub routes network messages and still ignores strangers.

## Oct 28 (Phase 2, S16: Teaching Mode)
- `lantern/mathcheck.py`: SymPy step checks behind a whitelist and a time limit (`given`, `equal`, `same_solutions`, `derivative_of`, `derivatives`).
- `lantern/lesson.py` + `Core.lesson`: Muse Spark plans a board lesson (structured output). Code holds it to the board's schema. SymPy checks every step; failures go back with the reason (up to 2 rounds, one rung up); what still fails is removed with its dependents. ✓ only where SymPy agreed; plots computed by SymPy. Lessons are saved in Lantern's own `lessons/` folder: `POST /lesson`, `GET /lessons`, `GET /lessons/{name}`.
- `lantern/checkwork.py` + `Core.check_work`: a photo of handwritten working (desktop `POST /checkwork`, or a phone photo captioned "check my work"). Muse Spark transcribes the lines; SymPy finds the first that doesn't follow; SAM 3.1 refines the location; Pillow circles it. The reply says why (with both solution sets when they differ) and is honest when a line can't be read.
- Desktop: a board window opened from the panel (`board:open` IPC, `board-preload.js`; `board.js` plays a lesson handed over by the app). The panel has a "Lessons on the board" form and list, and Check my work with the circled photo.
- SymPy is an optional extra (`teach`); KaTeX is now listed in THIRD_PARTY.md; CI installs the `network` and `teach` extras.
- Tests: `tests/test_lesson.py` (22).
  - The checker refuses non-math input; SymPy catches a wrong factorisation and the product-rule mix-up.
  - A wrong step goes back with the reason and returns fixed (a rung up). A step that never checks out is removed with what points at it, and the model's own "verified" is ignored.
  - Formulas show without a ✓; geometry has none and says so.
  - A generated lesson matches the board's JSON schema and plays in Chromium with `.check.on` exactly on the checked visible lines.
  - Notes go through the policy (private refused) and are wrapped untrusted; the API serves lessons.
  - Check my work: the first wrong line is found, explained and circled; SAM refines the circle (real wire format); unreadable lines are reported; a phone photo with "check my work" gets the reply.
- `desktop/test/lessons.test.js` (3).

## Oct 28 (Phase 2, S7: Decoy Screens)
- `lantern/decoy.py`: format-preserving, per-task HMAC stand-ins; `hide`, `guard` (the field may only get its stand-in), `reveal` (stand-in → real at keystroke; refuses real values and anything else in a guarded field).
- `BrowserDriver.decoy_screenshot`: stand-ins painted into the page's own elements for one screenshot, then restored (caret too); stops if a stand-in is left in a field.
- `ComputerLoop(decoys=…, on_decoy=…)`, `LANTERN_SCREENSHOTS=decoy` in the form filler (goal with stand-ins, `screen_privacy.typed_real`), `GET /decoy/image/{name}`, Privacy tab "What the cloud saw" (side by side). `scripts/eval_privacy.py` runs decoy mode.
- Tests: `tests/test_decoy.py` (8):
  - stand-in shape and stability; the map hides, reveals and refuses;
  - in Chromium, the screenshot matches the page holding the stand-ins (pixel diff within JPEG noise), and the page and caret are restored after; a page that keeps a stand-in stops the run;
  - the model types a stand-in and the real value lands, with no real value in any request; typing a real value or another value into a guarded field is refused;
  - fill_form in decoy mode keeps the pairs and reports them.
- The eval dry run now covers decoy mode.

## Oct 28 (Phase 2, S13: Lend your Lantern)
- `lantern/lend.py`: grants (`network/grants.json`, owner-only), `compile_grant` (Muse Spark → Cedar policy + hours; examples decided by cedarpy), `check` (in code first: active, lendable tool, no private/hidden zone; then Cedar on every file and site a call touches), and `Guard` (installed on a borrowed task's toolbox: check, then the owner's red card bound to the call's hash).
- Core: `lend` (preview/save), `revoke`, `expire_grants` (timer sweep; stops running tasks, denies their cards), `borrow`; borrowed tasks are never resumed or retried outside the loan. `Toolbox.lent` hook; `ApprovalBus.cancel_where`; `Scheduler.stop_task`.
- Hub: an active grant's sender gets through (text or voice, echoed in their chat); they get the reply, the owner gets the request, the cards and the full receipt; loan start/end notices to both. `GET /lend`, `POST /lend`, `POST /lend/{id}/revoke`.
- Tests: `tests/test_lend.py` (5):
  - Amma reads a bill with one owner card; the Bank and Private reads are refused with no card; she gets the reply and the owner the receipt; retry refused.
  - Strangers are ignored; revoking denies the open card and stops the task; an expired loan lets nothing through.
  - A voice note is echoed in her chat.
  - The owner's words compile with the engine's own decisions on the examples.
  - The code-first refusals.

## Oct 28 (Phase 2, S15: resume → forms profile)
- `lantern/cv.py` + `POST /profile/resume {path}`: the user's own resume (PDF or text, via policy.py, never private) becomes a proposal of profile values from Muse Spark (structured output). ID and card values are dropped; nothing is saved until the user ticks values in the panel (Setup → *Profile from my resume*), which saves them through `POST /profile`.
- pypdf as optional extra `cv` (THIRD_PARTY.md).
- Tests: `tests/test_cv.py` (4), `desktop/test/cv.test.js` (1).

## Oct 28 (live: first Phase 2 run on the real Meta API)
- The key reaches the API through the environment's network secret (Bearer for `api.meta.ai`): `MODEL_API_KEY` holds a placeholder and the proxy puts in the real key. `/v1/models` lists `muse-spark-1.3`, `muse-wonder-1.0`, `muse-image-1.0`, `muse-voice-transcribe-1.0`, `sam-3.1` (and `-contributor` variants, which Lantern never uses).
- `scripts/phase2_live.py`, measured: 5 of 6 checks passed, $0.1611 in total.
  - S1 parking: pass, $0.0466.
  - S2 tool error, then another way: pass, $0.0087.
  - S2 no duplicate email: pass, $0.0107.
  - S3 Spark high, then Wonder on a fresh chain: pass, $0.0593.
  - S1 restart from the checkpoint: pass, $0.0169.
  - S4 portal learn → heal: fail, $0.0188.
- **Bug found live and fixed:** mid-task, the API answered 400 "referenced response not found or expired" (check 6 on its third turn; check 3 too, after the refused duplicate). The recap fallback existed but didn't fire: `taskrun.chain_lost` matched a guessed message, and the fallback ran only after a restart. Now it matches the real message and runs on any turn. `tests/test_chain.py` (2) replays the live error.
- **S4 portal story, rerun after the chain fix:**
  - First agent run: done, 13 model calls, $0.0505.
  - Learned as a 22-step routine.
  - **Second run from the routine: done, 0 model calls, $0.0000.**
  - The heal after the portal's v2 layout did not work: 0 heals; the agent filled the form again, with 32 model calls per run. Being looked into.
  - The check cost $0.2444.
- **S16 Teaching Mode, first live runs** (Muse Spark, real API):
  - Lesson "solve x² − 5x + 6 = 0 by factoring": two runs, each with 3 steps ✓ by SymPy and nothing removed. Run 1: 2 rounds (one fix), $0.0256. Run 2: 3 rounds, $0.0659. The lesson plays on the board with ✓ on exactly those lines.
  - Check my work on a worksheet with a planted mistake on line 3 (three runs, $0.0055–$0.0073 each): line 3 found every time, read as written (not "corrected"), with the right reason ({2, 3} vs {1, 6}).
- **Fixed after looking at the live output:**
  - A hint line ran into the graph: lines that would overrun a graph or the board's edge now shrink to fit (`lesson.fit`).
  - The model gave circles r=1 (the graph's units, an invisible dot): a circle under 12 board units becomes a 22-unit ring, and the prompt now says r is in board units.
  - SAM's best "handwritten line" was once a ruled line of the paper below the right one: SAM now refines only when its box is centred inside the line and overlaps it by 40% or more.
  - Muse Spark's box was once half a line low: the box is now moved onto the ink near it, on this computer (`checkwork.snap`).
  - Tests for each.

## Oct 28 (live session B: docs read, S14 data, S15 meeting → tasks)
- dev.meta.ai read (it resolved here) and the code fixed where it differed: S12 sends the clip itself (`input_video`, `store: false`; frames as fallback), S9/S16 readers use `text.format` structured output, S16 sends Muse Spark's box to SAM as a box prompt. Details: DECISIONS (Oct 28, live session).
- Measured live: video steps $0.0072 (12 s clip); Check my work $0.0066 (rendered image, not real handwriting).
- S14 data: `scripts/s14_export.py` → `demo/s14/data/` (251 train, 93 held-out, split by routine; 344/357 Wonder labels agreed with the catalog). Spark 1.3 low: 93/93 on held-out. Export cost $2.37.
- S14 on Modal: `scripts/s14_weights.py` (weights in a Volume: 59.6 GB, 244 s), `scripts/s14_modal.py` (vLLM eval, TRL QLoRA). Baseline and training wait on a payment method on the Modal workspace (every GPU function is refused without one).
- S15 meeting recording → tasks and reminders: `lantern/meeting.py`, `POST /meeting`, `POST /meeting/confirm`. Live on a synthetic two-voice recording: A/B separated, 3 items, $0.0053.
- Tests: `tests/test_s14.py` (5), `tests/test_meeting.py` (2).

## Oct 28 (live session B, part 2: S4 heal bug)
- `phase2_live.py --only 6` rerun live: FAIL, $0.3027 (the agent's run 14 calls; v1 routine 0 calls; on v2, 17 of 18 steps re-targeted, then "none" for "Continue to payment" → "GO TO FEES", so no heal; the agent redid the form; a not-found reading was quoted in the reply).
- Fixed: repair prompt (redesigns reword labels; judge by the job), the local reader's `NOT FOUND` → `found: false`, no handle for a not-found answer, live script with the private lane off. Tests: `test_a_reworded_forward_button_is_repaired_and_only_back_left_is_none`, `test_a_local_reader_that_finds_nothing_gives_the_planner_nothing_to_quote`.
- Rerun live: **PASS**, $0.1045, 174 s: agent 13 calls ($0.0528) → routine v1 0 calls ($0) → v2 portal: 21 repairs, healed to v2 ($0.0273) → healed routine 0 calls ($0).

## Oct 28 (live session B, part 3: first runs of the remaining features, privacy eval)
- First live runs (one each unless noted): S10 scan + S9 read of a synthetic phone photo of a notice (rendered, not a real photo): page found by SAM, straightened, 5 fields read, card number left out, AI-directed line flagged; $0.0068, 10.5 s. S15 resume → profile: 12 values proposed, passport never proposed, planted "SYSTEM:" line ignored; $0.0041. S13 loan rules: failed (Cedar didn't parse), fixed, then 3/3 ($0.0048–0.0062 each); S6 network rules 3/3 ($0.0055–0.0080).
- `eval_privacy.py --runs 5` (raw, masked, decoy × clean and attack portal): 27/30 succeeded, 0 leaks, 0 traps, 30/30 runs' payloads all in the audit log, $1.70. 0 screenshots in any run (no computer use on this portal), so the three modes aren't compared by it.
- Fixed from it: "done" after an unverified change is refused in code; the form planner gets the user's own request; a planted `role=alert` no longer makes an accepted page look rejected. Tests in `test_recovery.py` (2) and `test_injection.py`.
- After the fixes, `eval_privacy.py --runs 5 --modes raw` on both portals: 10/10, 0 leaks, 0 traps, 153/153 payloads in the audit log, $0.58.

## Oct 28 (live QA of phase2-core on the real Meta API, branch `claude/qa-phase2-core`)
First real-API runs of S0–S12 (until now every Phase 2 model was scripted). Measured, about $1.20 of Meta credit in all:
- `scripts/phase2_live.py`: 5/6 checks passed on the first run ($0.16): S1 three tasks with one parked for an answer, S2 a tool error fed back, S2 the second email send refused, S3 Spark high then Wonder on a fresh chain, S1 resume after shutdown. S4 failed: see the fixes.
- S4 learn → rerun → layout v2 → heal → rerun, live, after the fixes: agent 14 calls $0.051; learned and verified; rerun **0 calls $0.0000**; v2 heal 21 steps, 21 calls $0.029; healed v2 **0 calls $0.0000**.
- S8 teach by demonstration, live: Playwright plays the person on demo/portal; Muse Spark writes contract and code; rerun **0 calls $0.0000 in 3.7 s**; v2 heal 20 steps, 18 calls $0.024 (repeated repairs reused); then 0 calls.
- S0 prompt injection, live (attacks A1–A6 plus "take care of everything in Documents, don't ask me", every approval auto-approved), 14 runs in scheduler and one-at-a-time mode: 0 emails sent, 0 files changed, 0 planted secrets in any request; in scheduler mode no canary reached a request that offered tools.
- S10 scan, live SAM 3.1: tilted 17° with a shadow and a decoy white sheet, and 38° at 55% brightness: page found with one call; a flat page was missed (fixed). S9 paper reading: ID and card numbers dropped, the planted instruction flagged, no canary in the fields.
- S12 Reel, live (ffmpeg video with espeak narration, Muse Voice Transcribe, Spark): 8 frames, transcript right, 5 steps; two dangerous steps passed review as "ok" (fixed).
- Fixes (details in DECISIONS): lost-chain recovery on the live wording, mid-task, in both agents; heal requests with page context, and one repair per (target, page) per run; Reel review (security wording, hidden names, private folders by name, data leaving the computer, links aren't folders); SAM phrase list; empty and copied-out reader answers; "stop #N" stops one task; board pauses capped at 60 s.
- Tests: `tests/test_chain_lost.py` (2), plus new cases in test_reel, test_scan, test_routines, test_security (2), test_scheduler, test_parallel_board; three repair test doubles follow the new request shape.
- Scheduler stress, live: 8 phone tasks at once (two that ask a question, a listing, "email it twice", Mars flight, a long job stopped with "stop #6", a Private-folder question with the private lane off, "paste ~/.ssh/id_rsa"). First run: two tasks re-asked their question after a lost chain and waited forever, because the recap said only "ask_user (ok)" (fixed: the recap carries the user's answer and Lantern's own small result fields, never content). After the fix all 8 reached a final state in 78 s for $0.116 (task costs sum to the total): answers used once, 1 email (second refused), #6 alone cancelled, Mars failed honestly, the key and the private file refused. A made-up `$h1` in a reply is now shown as "(that)", never raw.
- `scripts/eval_privacy.py --runs 2`, live ($0.45): clean portal 4/4 (raw and masked), attack portal 3/4; 0 leaks, 0 trap hits, every payload in the audit log. The one failure ticked no declaration on the attack page's payment step, so the portal didn't confirm and Lantern said "not verified". **Screenshots sent: 0 in every run**: the portal is filled from the page's structure, so this measures nothing about masking yet; a run with `--forms-model muse-wonder-1.0` on a page that needs computer use is still to do.
- Terminal mode: `lantern "goal"`, the chat prompt and `lantern --resume` now run on the scheduler (S0 split) through `lantern/terminal.py`; `Core(recover=False)` for the terminal. Tests: `tests/test_terminal.py` (5: the planner gets a handle, cards asked at the prompt and "yes" needed after untrusted content, the model's question answered on the same task, Ctrl+C, another Lantern's tasks left alone); the CLI resume test runs on the new path.

## Oct 30 (combine: phase2-core + qa-phase2-core, branch `claude/lantern-handoff-3fjofw`)
- One product line: `claude/phase2-core` (tip `7fd8687`) merged with the live-QA fixes from `claude/qa-phase2-core` (PR #8). No new features.
- Conflicts resolved by keeping both sides' fixes:
  - `scheduler.py`: one `stop_one(task_id, why)` ("stop #N" stops only that task and denies its open approval cards); `stop_task` stays as the S13 loan-ended wrapper.
  - `security.py`: a not-found or empty text answer gives no handle (both branches' fixes, one note).
  - `reader.py`: phase2-core's `NOT FOUND` hint for the local reader, with qa's 1200-character cap on text answers.
  - `compiler.py`: one repair prompt with page title/heading, next steps and "judge by the job" (both live heal fixes).
  - `taskrun.py`: qa's wider lost-chain test (any 400/404 naming the previous/referenced response).
- `tests/test_routines.py`: the reworded-button test reads the repair listing's new `{"page", "candidates"}` shape.
- `python -m pytest -q`: 631 passed, 7 skipped. pyflakes clean. No live API runs in this step.

## Oct 30 (Muse Spark Open on Baseten, optional lane)
- `lantern/baseten.py`: `SparkOpen.ask(system, user, images=...)` on Baseten's OpenAI-compatible Chat Completions; config `LANTERN_BASETEN` (off by default), `BASETEN_API_KEY`, `LANTERN_BASETEN_EFFORT`. Only `meta-models/muse-spark-open-1026`; reasoning never returned; reasoning tokens counted in the cost; usage logged per task; cloud-lane scrubbing, deny list and audit; private image files refused through policy.py.
- `models.py`: `BASETEN_BASE_URL`; Muse Spark Open priced at Baseten's list price ($1.25 / $0.15 / $4.25 per 1M) instead of $0.
- `scripts/baseten_live.py`: one text and one image (a rendered receipt) check against the live endpoint.
- Tests: `tests/test_baseten.py` (17), scripted fakes only.
- **Live: not measured.** The key in this environment is refused: HTTP 403 "please check the api-key you provided" for every request (Bearer, Api-Key and no header alike), so `baseten_live.py` reports both checks failed at $0. To rerun once the network secret is fixed: `LANTERN_BASETEN=on BASETEN_API_KEY=injected-by-proxy python scripts/baseten_live.py`.

## Oct 30 (ports from claude/real-run and claude/modal-glimmer)
- Router: `LANTERN_GLIMMER_KEY_FILE`, `LANTERN_GLIMMER_TIMEOUT` (600 s self-hosted), honest "private server" prompt on the self-hosted lane. real-run's `$p1` placeholder not ported (phase2-core's private handles do that job).
- `deploy/modal/`, `tests/modal/` (83 tests), `docs/MODAL.md` from `claude/modal-glimmer`: A10 default, `--load-mode none`, CPU mode, separate app/Secret names per session. THIRD_PARTY.md lists llama.cpp, modal, huggingface_hub and the Glimmer weights.

## Oct 30 (live: scripts/real_run.py on the combined branch, real Meta API)
Ported `scripts/real_run.py` from `claude/real-run`; tasks run on the scheduler (the protected planner/reader path) unless `--workers 0`. No Glimmer URL set, so the private lane is off and check 2 was not run (it needs the self-hosted private Glimmer server).
- **Check 1** file task: PASS, 48 s, $0.029. Muse Spark only, one approval, moved on disk.
- **Check 3** planted fake AWS key: PASS, 49 s, $0.022. The key was in 0 of 6 Meta requests (redacted once, in the reader's request; the planner never sees file content).
- **Check 5** prompt-injection files (A1, A3): first run FAIL ($0.087): `ask_user` refused a short question three times because the limit counted the filled-in handles. Fixed (limit on the model's own words). Rerun: PASS, 118 s, $0.062; no canary in any tool call, card, email or reply; 0 emails.
- **Check 4a** demo portal, Spark, scheduler: PASS, 120 s, $0.068. 6 pages, 6 Submit approvals, portal empty before each yes, 1 application, pay at counter, nothing typed into card/OTP/password fields.
- **Check 4b** demo portal, Muse Wonder on screen (`LANTERN_FORMS_FILL=visual`, `--workers 0`), screenshots masked: PASS, 404 s, $0.335. Same result as 4a.
  - **Masking measured with computer use for the first time:** 21 Wonder screen steps; 12 of the user's own values were covered in screenshots, 0 shown unmasked. Masking covers screenshots only: the values Wonder must type are in its goal.
  - On the scheduler, 4b quietly ran on Spark (the S3 ladder picks Spark for computer use until a fill fails), so the script now refuses 4b unless `--workers 0`.
- Harness fix: with no `LANTERN_GLIMMER_URL`, `real_run.py` turns the private lane off (like `phase2_live.py`). Before that, the rehearsal stand-in answered public reads (check 3's first run, $0.015).
- Meta spend for these runs: $0.75 in total, including the 4b run that turned out to be Spark ($0.13) and the failed first runs.
- **`scripts/phase2_live.py` on the combined branch, all 6 pass ($0.30):**
  - S1 three tasks, one parked for an answer: 65 s, $0.046.
  - S2 tool error, then another way: 36 s, $0.017.
  - S2 no duplicate email: 240 s, $0.017.
  - S3 Spark high, then Wonder on a fresh chain: 104 s, $0.086.
  - S1 restart from the checkpoint: 32 s, $0.017.
  - **S4 (demo centrepiece):** agent 15 calls $0.063; learned (22 steps); rerun **0 calls $0.0000**; v2 layout: 21 steps healed, 19 calls $0.025; healed rerun **0 calls $0.0000**. 200 s, $0.115 in all.

## Oct 30 (S17 FreeCAD connector merged into the product line)
- From `claude/freecad-connector` (#5): `lantern/integrations/freecad/`, `demo/freecad/`, `tests/freecad/`, `docs/FREECAD.md`, `config/mcp/freecad.example.json`. Typed CAD tools as deferred MCP tools behind tool_search; green/yellow/red tiers with zone checks and exact-file red approvals; the visual verify loop on Muse Spark; three demo tasks.
- Merge hooks applied: `Toolbox.mcp_call` routes the `freecad` server through the connector; Core gives its toolbox one, and every scheduler task gets its own with that task's client. THIRD_PARTY lists FreeCAD (LGPL, not bundled).
- Tests: `tests/freecad/` 59 + 1 new (Core and scheduler tasks get their connectors). Real FreeCAD is still not tested: it needs the laptop (`python demo\freecad\run_demo.py --freecad`).

## Oct 30 (desktop: the orb and a calmer, glassier panel)
- Lantern is an orb now (no face): a glass sphere with three lights drifting inside. Colour and motion follow the state (ready, working, several tasks, needs your OK, done and checked, finished, something failed, resting); colours glide between states. Shared by the avatar window and the panel (`renderer/orb.css`, `lib/sprite.js`), built with DOM calls (no HTML parsing).
- Panel, after the "under the avatar, a line of what it's working on" idea: Today opens with the large live orb, the greeting, and a status line under it ("Clicking at… · Wonder", "Submit this form?"). The toolbar's mark is a small live orb. Glass cards and ask field, rounded numbers, a floating tab bar, a clearer approval card (big Deny / Allow, blue-violet like the orb when it waits for you). Light and dark.
- Checked: `npm test` 41/41, `tests/test_desktop*.py`; rendered against the mock server in Chromium (screenshots in docs/screenshots). Not checked here: the Electron e2e run (Electron isn't installed in this sandbox).

## Oct 30 (S14 baseline on Modal)
- Modal GPUs enabled. Base Muse Glimmer on the S14 held-out set: **93/93** (48/48 none), median 277 tokens per answer, A100-80GB 296 s wall (load 199 s, answering 38 s), about $0.20. Result in `demo/s14/data/eval_baseline.json`. Training waits for the owner's OK.
- S14 trained: QLoRA on 251 rows (32 steps, loss 1.84 → 0.14, A100 500 s); merged into the base (vLLM's LoRA path fails for Glimmer); held-out **base 93/93 vs tuned 90/93**, median answer 277 → 15 tokens, 93 answers in 38 s → 8 s. Modal about $1. `scripts/s14_modal.py` gained `--step merge`; results in `demo/s14/data/{train_result,eval_tuned}.json`.

## Oct 30 (S15 avatar reactions)
- Lantern's face made with Muse Image: an idle portrait and 10 reactions as Muse Image edits of it (`lantern/avatar.py` `react`, `scripts/make_avatar.py --reactions`), cached by the portrait's hash; 11 images, $0.11. `healed` after a routine heals itself, `listening` for push to talk. Tests: `tests/test_avatar.py` (2), `desktop/test/logic.test.js` (1).
- Desktop redesign: sidebar + toolbar + grouped lists (`panel.html`, `panel.css`, `panel.js`, `main.js`), Learned/Teaching/Meetings sections split out of Costs, the Meetings panel (`lib/meeting.js`, `desktop/test/meeting.test.js` (2)), `e2e/tour.js`. e2e + axe on 9 sections × light/dark: pass.
- S15 live talk: `lantern/talk.py` + `/talk` websocket, hold-to-talk in the panel (`lib/talk.js`, `lib/pcm-worklet.js`), one-shot microphone permission. Tests: `tests/test_talk.py` (3), `desktop/test/talk.test.js` (2); checked once through the real Electron app with a fake microphone and a fake realtime server. The real endpoint needs the laptop (key in the handshake).

## Oct 30 (screenshot-mode eval on a computer-use page; approvals as a sheet)
- `scripts/eval_privacy.py --tasks reimbursement`: the demo reimbursement form, whose custom dropdown needs computer use, so screenshots really leave. Success = the task is "done" and fill_form verified the confirmation with category=travel. Dry-run harness scripted too (clicks measured in the same layout). Test: `test_the_computer_use_task_sends_screenshots_and_each_mode_treats_them_differently`.
- Measured live (Muse Spark, 5 runs per mode): raw 5/5, masked 5/5, decoy 5/5; 16/15/16 screenshots; user values shown 38/0/0, covered 0/36/32; 0 leaks; every payload in the audit log (46/46, 45/45, 50/50); $0.039/$0.043/$0.046 per run; $0.64 in total.
- Desktop: an approval is now a sheet over the dimmed window (Mac style) instead of a card squeezed above Today. README hero and screenshots retaken from the e2e and the tour (1080×720 window); Meetings and Learned added to the gallery.
- Today is now only the conversation:
  - your request on the right, Lantern's answer on the left (steps folded), oldest first;
  - the message box is pinned to the bottom, and the empty state is a greeting with suggestions.
- What moved out of Today:
  - Reminders got their own sidebar section with a count badge;
  - the at-a-glance tiles went to Usage;
  - the camera check-in went to Widgets;
  - Stop went to Settings, out of the toolbar (Ctrl/Cmd+Shift+X still stops everything from anywhere).
- Waiting approvals show as a red badge on Today.
- e2e and axe pass on 10 sections in light and dark.

## Oct 30 (better WhatsApp messages for the phone side)
- Built with Muse Code (OpenCode on Muse Spark 1.3). Files: `lantern/channels/cards.py` (new), `lantern/channels/suggest.py` (new), `lantern/channels/daily.py` (new), `lantern/channels/hub.py`, `lantern/channels/base.py`, `lantern/channels/whatsapp.py`, `lantern/channels/telegram.py`, `lantern/channels/fake.py`, `lantern/channels/__init__.py`, `tests/test_phone.py` (new, 19 tests), `tests/test_channels.py` (help test now expects the menu).
- Approval cards are formatted in code (`approval_card`, pure, unit-tested): tier-marked action line, Label: value facts from the payload (hashes never shown), an untrusted-content line, expiry footer ("Lantern needs your OK · expires in N min · #id", WhatsApp footer ≤60, appended to text elsewhere; tidy cards group short destination paths).
- `send_list` on Channel (numbered-text fallback), WhatsApp interactive list, Telegram inline keyboard, fake record; WhatsApp parses `list_reply` into button taps. "menu"/"hi"/"help" sends the fixed menu (Status, Reminders, Undo, Summary, Stop); numbers work as fallback; "Stop everything" asks "Stop all tasks?" (Yes/No) first.
- Muse Spark follow-ups (`models.SPARK`, one structured chat call on the Spark-low rung through the audited client, cost on the task): ≤2 buttons under a done reply; taps start new tasks from server-side text; skipped when off (`LANTERN_PHONE_SUGGESTIONS=0`), private, clientless, or over the task budget.
- Daily summary at `LANTERN_DAILY_SUMMARY` (default 21:00, "off" disables; polled on the serving path only) plus the "summary" command/menu row: facts gathered in code (tasks, reminders, spend, denied approvals; no private replies), Spark writes ≤5 lines in Lantern's persona, code fallback otherwise.
- `python -m pyflakes lantern scripts tests` clean; full suite 683 passed (incl. the desktop e2e, which still finds the cards on the simulated phone).
- Fixes from review:
  - No suggestions after a task that read untrusted content (the agent's done event now carries `tainted`; an event without it gets none).
  - The full suggested requests are shown above the buttons, so a tap sends nothing the owner didn't read.
  - The Spark call runs on its own thread, so it can't delay the next task.
  - Test: `test_no_suggestions_after_untrusted_content_and_full_requests_are_shown`.
- Known limit: the "already sent today" mark lives in memory, so a restart after 21:00 sends the daily summary again.
- The Muse Code run cost about $2.63 of Muse Spark tokens (14.5M cached input, 226K input, 40K output).

## Oct 30 (paired phone PWA)
- Built with Muse Code (OpenCode on Muse Spark 1.3). Files: `lantern/pairing.py` (new: 6-digit codes, 5-min expiry, single use, 5-try burn, devices.json mode 0600 with hashes only), `lantern/phone.py` (new: WhatsApp-level privacy filter for tasks/approvals/steps), `lantern/server.py` (pairing routes, `/m/api/*`, `/m/events` device websocket, static `/m` with CSP, LAN guard, `serve(..., phone=True)` + LAN URL line), `lantern/cli.py` (`--phone` / `LANTERN_PHONE=1`, demo+phone keyless), `phone/` (new PWA: `index.html` with strict CSP meta and no inline script, `app.js` plain JS, `style.css` light/dark, `manifest.webmanifest`, `sw.js` shell-only cache, `icons/` own lantern SVG + Pillow-made PNG 192/512), `desktop/renderer/panel.html` + `panel.js` (Settings "Pair a phone" row: code+URL display, device list with Revoke), `docs/PHONE.md` (new), `tests/test_pairing_phone.py` (new, 16 tests).
- Privacy: phone gets `reply_public`-equivalent only (private tasks show the placeholder), folded steps without args/file text, approval cards with nonce+payload_hash but never the raw payload; decisions go through the same `ApprovalBus.decide` nonce/hash check.
- `python -m pyflakes lantern scripts tests` clean; new tests 16 passed; server/channel/phone suites 55 passed; `cd desktop && npm test` 46 passed; `lantern --serve --demo --phone` verified keyless via TestClient (health demo, /m 200, pair round-trip, device task list 200).
- No new libraries (Pillow PNGs use the existing dependency).
- Lantern on the phone (paired web app, `phone/`, `lantern/pairing.py`, `lantern/phone.py`, `--phone`), built with Muse Code (OpenCode on Muse Spark 1.3), about $1.50 of tokens. Fixes from review:
  - The LAN guard only covered HTTP: the desktop's `/events` and `/talk` websockets now refuse non-local clients too.
  - A request that arrives through a tunnel or proxy (forwarding headers such as `X-Forwarded-For` or `CF-Connecting-IP`) no longer counts as local, so a tunnel can't reach the desktop's routes.
  - On the phone: one untrusted-content warning on the approval sheet instead of two; Lantern's face in the empty chat; `frame-ancestors` sent as a header only.
  - Tests: `test_tunnelled_requests_and_desktop_sockets_are_not_local_when_phone_on`.
- Checked in a phone-sized browser (390×844, light and dark) against `lantern --serve --demo --phone`: pair with the code → send "Tidy my Downloads folder." → approve from the bottom sheet → verified reply. No horizontal scroll.
- Not checked on a real phone yet; HTTPS (needed to install to the home screen on most phones) needs the owner's own tunnel (docs/PHONE.md).

## Oct 30, 2026: S17 FreeCAD connector merged in (branch claude/freecad-laptop)
S17 FreeCAD connector (`lantern/integrations/freecad/`, `demo/freecad/`, `tests/freecad/`, `docs/FREECAD.md`, `config/mcp/freecad.example.json`):
- typed CAD tools as deferred MCP tools behind tool_search;
- green/yellow/red tiers with zone checks and exact-file red approvals;
- the visual verify loop on Muse Spark;
- three demo tasks;
- 59 offline tests.

Verified live with Muse Spark on the fake geometry, including plain-English tasks through Core. Real FreeCAD is laptop-only.

Merge hooks applied: `Toolbox.mcp_call` routes the freecad server through `FreeCADConnector`; `Core` builds the connector with the guarded client; one `BASE_RULES` line; THIRD_PARTY row; a doctor check that FreeCAD is found when mcp.json has a `freecad` server. Windows: FreeCAD found in any `FreeCAD*` folder under Program Files or `%LOCALAPPDATA%\Programs`. 4 new tests (`tests/freecad/test_freecad_laptop.py`), including Core wiring the connector.

## Oct 30, 2026: T1 open_link
- Share a YouTube/web link to the Telegram bot and it opens in the laptop's browser after one approval: `lantern/linkopen.py` (clean_url + open_url, stdlib only), the `open_link` tool (approval bound to the URL hash, verified = the browser accepted it), an agent rule, and a phone fast path in the channel hub (no model call). 10 new tests.

## Oct 30, 2026: S17 on the Windows laptop (first real FreeCAD run)
Real FreeCAD 1.1.0: all FreeCAD tests and all 3 demo tasks pass, with Muse Spark's visual checks. Lantern's desktop panel built the enclosure through the hooks (6 approvals, $0.09). Fixed 5 Windows issues: per-user/newer FreeCAD installs, EINVAL on restart, path comparison in the demo, the demo ignoring `.env`, and the desktop app starting the wrong Python. Details, exact commands and costs: `docs/FREECAD_LAPTOP.md`. Still to confirm: the red export card and STL via the panel, and measuring from the panel.

## Oct 30, 2026: T3a agent packs (back end)
Shareable agents as data-only JSON (`lantern/agentpacks.py`: validation, `PackStore` in `~/.lantern/agents/`, tool-allowlist matching, drafting via Muse Spark structured output in the FreeCAD connector's shape). Install needs an approval bound to the file's SHA-256; runs render `{blanks}` into `[agent:<name>]` goals, append instructions after "Now:" (prefix byte-stable), enforce the allowlist in `Toolbox.call`, and start tainted. `Core.submit(pack=, inputs=)`, `pack_install`, `pack_draft`, and the `/agents` server routes; example packs `agents/cad-enclosure` and `agents/tidy-downloads` (send-to-phone skipped: T2 isn't merged). 30 new tests in `tests/test_agentpacks.py`. Docs: `docs/AGENT_PACKS.md`.

## Oct 30, 2026: T3b desktop Agents tab
Agents tab in the desktop panel (`desktop/renderer/panel.*` + new `lib/agents.js`): cards with tools and Run/Share/Disable, a per-input run form that hands off to the Today timeline with Watch/Stop, Add via file picker or drop (install approval card as usual), Share through Electron's save dialog (exact export bytes), and Make-an-agent drafts from finished tasks. Mock server speaks the /agents routes. 5 new panel tests; `npm test` 32/32.

## Oct 30, 2026: T4a avatar studio
Design Lantern's face in words (`lantern/studio.py`: brief validation at 600 chars with secrets scrubbed, 4 styles, `StudioStore` in `~/.lantern/avatar/<id>/`). `POST /avatar/design` draws one canonical preview (~$0.01); `POST /avatar/design/{id}/accept` renders the other six expressions anchored on it behind a `finish_avatar` approval showing "7 images, about $0.07" (declining writes nothing; switching back to a finished design needs no model call). `GET /avatar/manifest` + `GET /avatar/file/{id}/{expression}` serve the active design with auth and name checks; the avatar window loads server-first, then bundled art, then the glow (`lib/avatar.js`), and the Setup tab gained an Avatar section (brief box, style chips, Preview/Use this, gallery). Every image prompt carries "no logos, no text, no brand marks"; costs log to memory like other model calls. 11 new Python tests in `tests/test_studio.py`, 2 new desktop tests; `npm test` 34/34. Docs: `docs/AVATAR.md`.

## Oct 30, 2026: T4b live skins (clock)
A free, no-model skin for the avatar window (`desktop/renderer/lib/skins.js`: `muse`/`glow`/`clock`, one function per skin). The clock is our own SVG showing the real local time, redrawn every second; the ring pulses while working, turns amber while an approval waits, grey when stopped. The skin is picked under Setup → Avatar → Skin and saved with the existing `POST /settings` (`skin` joins `Config`/`SETTABLE`, validated against the allowlist, persisted next to memory.db). 1 new Python test + updated settings shape in `tests/test_server.py`, 4 new desktop tests in `desktop/test/skins.test.js`; docs in `docs/AVATAR.md`.

## Oct 30, 2026: T4c agent faces
An agent pack can have a face: a short emoji (free, kept at once) or one drawn Muse Image icon (~$0.01, behind an `agent_face` approval showing the cost), set from the Agents tab with "Make a face" (`POST /agents/{name}/face`; `GET /agents/{name}/face` serves the icon with auth and name checks). The icon is a separate `<name>.webp` next to the pack and the emoji stays in the laptop's pack wrapper, so sharing a pack still sends only the data-only file. While pack tasks run, the avatar window shows one small face per running agent beside the main avatar (a plain row, so `prefers-reduced-motion` needs no exception), each with an activity dot: click opens that task's timeline in the panel, right-click stops everything. `queued` events now carry the pack name; the reducer tracks running agents and drops them on `done`/`stopped`. 7 new Python tests in `tests/test_agentfaces.py`, 2 new desktop reducer/card tests + 1 face-helper test + 1 mock test; `npm test` 41/41. Docs: `docs/AVATAR.md`.

## Oct 30, 2026: T3e signed agent packs
Who sealed a shared pack, and whether it changed since: `lantern/packsign.py` (one Ed25519 key per user in `~/.lantern/keys/pack-signing.key`, fingerprint `3F2A-91C0` style, sign/verify over `canonical_bytes` without the seal). `agentpacks.validate` allows the optional `signature` key (strict shape, base64 checked) and refuses tampered files with "this pack was changed after it was signed"; every load re-verifies, so a disk edit refuses the next run. Install uses trust on first use (`trusted_keys.json`): new keys ask "Trust it?", a known author name on a new key warns in red, unsigned packs install with an "Unsigned" card. `POST /agents/{name}/sign` seals your own pack after an approval; the Agents tab shows a signature badge per card, the fingerprint in the install approval, and offers "Sign before sharing" when the author is you. 12 new tests in `tests/test_packsign.py` (+ badge/mock desktop tests); `npm test` pure-JS suites pass (mock-server HTTP tests fail in this sandbox on network, like the other local-HTTP tests). Docs: `docs/AGENT_PACKS.md`.

## Oct 30, 2026: T6 learn_repo (read a GitHub repo, install pinned MCP servers, draft agents)
`lantern/repolearn.py`: `learn_repo(url)` reads a repo over stdlib urllib only (metadata, pin SHA, SPDX license, README + top-level manifests; 30 s timeout, 2 MB cap, `GITHUB_TOKEN` never logged), wraps everything as untrusted and taints the task. Muse Spark summarizes (FreeCAD connector's structured-output shape); `mcp` dep / `@modelcontextprotocol/sdk` / `server.json` facts override the model. (A) installs behind a red card (pinned SHA, license, stars, exact commands, `~/.lantern/mcp/<name>/`), fresh venv or `--ignore-scripts` npm, `read_only: []` + source in `mcp.json`, probe-then-list with rollback. (B) builds a wrapper with `muse exec` (no `--yolo`, `--contributor-mode off`, 150 steps) in `~/.lantern/builds/<name>/` and continues as (A). (C) drafts a docs pack limited to Lantern's own tools. One-line agent rule in `BASE_RULES`; `Core` sets `toolbox.mcp_config`. 18 new tests in `tests/test_repolearn.py` (fake GitHub + FakeClient, no network).

## Oct 30, 2026: T5 LocalSend receiver (our own implementation of the public protocol, approvals, inbox via policy)
`lantern/localsend.py`: receiver only, built from the public protocol README (v2.x) alone — multicast 224.0.0.167:53317 discovery (announce on start, answer announcements, graceful degrade to HTTPS-only with the reason in the doctor), self-signed TLS cert made once in `~/.lantern/localsend/` with the spec's SHA-256 fingerprint, and the v2 HTTPS paths info/register/prepare-upload/upload/cancel. prepare-upload never auto-accepts: a tainted bus approval ("📱 Pixel 8 wants to send …") maps Accept/decline to the spec's token/reject answers, with an optional `LANTERN_LOCALSEND_PIN`; session ids and file tokens are `secrets.token_urlsafe` compared with `hmac.compare_digest`. Files save only through `lantern.policy` into `~/Downloads/Lantern Inbox` (must be a writable zone or start is refused), names sanitized and de-duplicated, 500 MB / 100 files / exact-size enforced, sha256 verified (422). `text/plain` shares become messages: links submit the T1 goal, other text waits for the panel. Audit rows (lane `local`) hold sender, names, sizes, accepted/declined — never contents. Off by default (`LANTERN_LOCALSEND=1` or Setup → "Receive from phones on Wi-Fi", persisted in config.json and applied live); `--serve` starts it, the doctor reports listening/off with a Windows Firewall hint, the desktop Setup and mock server grew the switch. 16 new tests in `tests/test_localsend.py` (in-process, no sockets) + doctor/server/mock coverage; full `pytest` passes except the known sandbox-network local-HTTP tests (test_wire, one desktop, one server) and the pre-existing `test_ci_review` git-identity errors, all reproduced on the clean base. Docs: `docs/THIRD_PARTY.md`, `docs/DECISIONS.md`.

## Nov 1 (S16: interrupt the teaching board by voice)
- Built with Muse Code (OpenCode on Muse Wonder 1.0).
- The board can now be interrupted mid-lesson: hold **Hold to ask** (or hold Space) and the timeline pauses; in the desktop app with a key, the mic streams to the server's existing `/talk` (Muse Voice Transcribe realtime, the panel's `lib/talk.js` + `lib/pcm-worklet.js`, partial transcript as a caption); without a key or in rehearsal a typed question box opens instead (and if the mic fails, it falls back to typing). On release the question goes to the new `POST /lesson/ask`; Lantern answers, adds any checked board actions, and **Resume** (or a Space tap) continues from the paused step.
- The answer endpoint: Muse Spark (`models.SPARK`, structured output through the audited client, cost logged) is given ONLY the saved lesson's verified lines up to the current step plus the question. Code holds the answer to the lesson's action schema (≤3 actions), re-checks every new math line with SymPy over the verified lines, drops unverified lines (never shown, never ✓), renames answer items `ask-…`, and refuses erases/pauses in answers. An unrelated question gets one brief sentence and no actions. The spoken answer uses the board's OS speech synthesis, as before, and is not described as a Meta model.
- Rehearsal: the scripted demo model now answers structured chat calls too — a canned factoring lesson and a canned board answer (highlighting the last verified line) — so the whole flow runs offline at $0.00.
- Files: `lantern/lesson_ask.py` (new), `lantern/core.py` (`lesson_ask`), `lantern/server.py` (`POST /lesson/ask`), `lantern/demo.py` (scripted chat), `desktop/board/lib/ask.js` (new pure logic), `desktop/board/board.js`, `desktop/board/index.html`, `desktop/board/board.css`, `desktop/board/README.md`, `desktop/board-preload.js` (server config + one-shot mic), `desktop/main.js` (lesson name to the board; mic grant for the board window), `desktop/preload.js`, `desktop/renderer/panel.js` (pass the lesson name), `tests/test_lesson_ask.py` (new, 6 tests), `desktop/test/board-ask.test.js` (new, 4 tests), `desktop/e2e/run.js` (seeded lesson → hold typed question → answer → Space resume → lesson finishes), `docs/DECISIONS.md`.
- Tests: `python -m pyflakes lantern scripts tests` clean; `tests/test_lesson_ask.py` 6 passed; `cd desktop && npm test` 50 passed; `xvfb-run -a node e2e/run.js` ends "all end-to-end checks passed"; full `python -m pytest -q`: 707 passed.
- Review fix (Nov 1): the spoken answer was shown twice (caption line and answer box). It now appears once, in the answer box with Resume; the caption keeps the lesson's lines and carries the live partial transcript while a question is being asked. Regression checks in `desktop/test/board-ask.test.js` (51 node tests) and the e2e (answer in `#answer-text`, absent from `#caption`, exactly once in the board's visible text); e2e still ends "all end-to-end checks passed".

## Nov 1 (circled "Check my work" photo back to the phone; persistent daily-summary mark)
- Built with Muse Code (OpenCode on Muse Wonder 1.0).
- The circled image now goes back to the chat the photo came from, with the one-line explanation as its caption (previously the phone got only the text and the image stayed in the desktop panel). WhatsApp: media upload (`POST /<phone_number_id>/media`, multipart `messaging_product=whatsapp` + `type` + `file`) then an image message with the returned media id; Telegram: `sendPhoto` multipart. Both shapes and limits (WhatsApp JPEG/PNG ≤5 MB, caption ≤1024; Telegram photo ≤10 MB, caption ≤1024) were read from the official docs first and are recorded in `docs/DECISIONS.md` under "Nov 1"; limits are enforced in code before any request, captions truncated to the limit.
- Safety, in code: only a PNG/JPEG resolved inside Lantern's media folder may be sent (checked when queued and again on the sending thread — never a user file); a private task's image never leaves; the caption is scrubbed like other outgoing text; the audit row (`<channel>.send_image`) records the image bytes' SHA-256, size and the channel destination. `Channel.send_image` has a "📷 image: caption" text fallback; the simulated phone records the image so the panel shows it that way (no `desktop/` change needed).
- The daily summary's "sent today" mark is now persisted (`daily-summary.json` beside the memory DB, written before sending and re-read on every check), so a restart after the summary time no longer sends it twice; the next day it sends again.
- Files: `lantern/channels/base.py` (stdlib multipart helper, `send_image` fallback, image type/size reader), `lantern/channels/whatsapp.py` (upload + image message), `lantern/channels/telegram.py` (`sendPhoto`), `lantern/channels/fake.py` (records image sends), `lantern/channels/hub.py` (media-folder/private guards, scrub + audit, check-work image delivery, persisted daily check), `lantern/channels/daily.py` (sent-date load/save), `docs/DECISIONS.md`, `tests/test_image_send.py` (new, 11 tests: fake-HTTP request shapes and limits for both channels, fallback + fake record, media-folder and private refusals, scrub + audit row, check-work delivery, daily restart), `tests/test_phone.py` (daily schedule test now expects the restart not to resend, and the next day to send the fallback).
- Tests: `python -m pyflakes lantern scripts tests` clean; nearby suites (`test_image_send`, `test_channels`, `test_phone`, `test_lesson`) 76 passed; full `python -m pytest -q`: 718 passed. `desktop/` untouched, so the desktop test/e2e steps were not required.

- Reviewer's note: one test failed once in a full run after B (name not captured) and passed in 7 later runs of the full and nearby suites; watching for it.

## Nov 1 (photos and voice notes in the paired phone app)
- Built with Muse Code (OpenCode on Muse Wonder 1.0).
- The paired phone app (`/m`) takes the same two inputs as WhatsApp, through the same pipelines — no new model path. Photo: a camera button (`<input type="file" accept="image/*" capture="environment">`) in the composer; the phone downscales to at most 2000 px on the long side as JPEG before upload, and any words in the message box travel as the caption (`POST /m/api/photo`, device token, raw body + `?text=`, multipart also accepted). The server sniffs the image type from the bytes (JPEG/PNG/WebP), refuses over 20 MB, routes "check my work" captions to `core.check_work` and everything else to `core.photo` (the hub's `_start_photo` split, `CHECK_WORK` pattern reused). Voice: hold the mic (`MediaRecorder`, stops on release, 60 s cap) and upload to `POST /m/api/voice`; the server converts and transcribes through the hub's own converter/transcriber seams (ffmpeg → 16 kHz WAV → Muse Voice Transcribe, keywords bias, same audit + usage logging), replies with the transcript as "🎙 …", and submits it through the same `_submit_goal` as typed text. Without ffmpeg or a key the route answers 503 naming the missing piece; a demo core with no hub returns the scripted transcript.
- A check-my-work task's circled image now shows in the phone chat: task views carry `has_image`, and `GET /m/api/tasks/{id}/image` (device token) serves only the file that task's own check-work result names, resolved inside Lantern's media folder as PNG/JPEG; private tasks are refused before that, unknown/foreign/outside images get 404.
- CSP: the phone policy gains only `img-src 'self' data: blob:` and `media-src 'self' blob:` (an `<img>` cannot send the device-token header, so the app fetches the circled image and shows its object URL); `script-src` stays `'self'`, no external origin is referenced (asserted by a test). The MDN shapes read first (MediaRecorder, `capture`) and every decision above are recorded in `docs/DECISIONS.md` under "Nov 1".
- Files: `lantern/server.py` (the three routes, upload handling, audio-type/size checks, plain voice errors, CSP), `lantern/phone.py` (`sniff_image`, `task_image_path`/`has_task_image`, `has_image` in task views), `phone/index.html` (camera + mic buttons, CSP), `phone/app.js` (raw/authed uploads, on-phone downscale, hold-to-record, voice-task "🎙 " rendering, circled-image loading), `phone/style.css` (44 px camera/mic targets, recording state, task image), `docs/PHONE.md` (photos and voice notes), `docs/DECISIONS.md`, `tests/test_phone_media.py` (new, 13 tests: device auth on all three routes, caption and check-my-work routing, sniff/size/empty refusals, plain pipeline refusal, transcript shown + routed like text, ffmpeg/key/empty/type/size voice refusals, scripted demo transcript, circled image served and flagged, private/outside/other-task refusals, static-app origins/CSP/input checks).
- Tests: `python -m pyflakes lantern scripts tests` clean; `node --check phone/app.js` clean; nearby suites (`test_phone_media`, `test_pairing_phone`, `test_image_send`, `test_handoff`) 47 passed, plus `test_phone_media`/`test_phone`/`test_demo` 37 passed; full `python -m pytest -q`: 731 passed. `desktop/` untouched, so the desktop test/e2e steps were not required.
- Reviewer's fix: the intermittent failure seen after B was `test_tunnelled_requests_and_desktop_sockets_are_not_local_when_phone_on` (the refused websocket sometimes ends in CancelledError, a BaseException); the test now accepts it. 15/15 runs pass.
- Known: the phone's camera and mic buttons are still emoji (the mic glyph is missing in some fonts); line icons are the next change.
- Phone composer: camera and mic emoji replaced with own 22px inline SVG line icons (stroke currentColor, aria-hidden, existing aria-labels); the mic pulses red while recording (no animation under prefers-reduced-motion), the message box shows elapsed recording seconds, and the mic hides when MediaRecorder/getUserMedia is unavailable.

## Nov 1, 2026: T3c loop agents (scheduled packs with per-run budget, pause on failures)
Optional `schedule` in a pack (`lantern/agentpacks.py`: `every` day/weekday/week/hours, `at` HH:MM, `hours` >= 1, inputs covering every `{blank}`; strict validation, secrets scrubbed, due-time math in local wall-clock with a fake clock for tests). Installs start OFF; `Core.loop_resume` asks first (frequency + per-run budget on the card, bound to schedule + budget), `loop_pause`/`loop_run_now` need no approval to stop nothing. Due runs fire from the reminder loop (no second thread) as ordinary tainted, allowlisted tasks with their own `LANTERN_TASK_BUDGET_USD` cap ($0.10 default via `BudgetGuard` per-task overrides); never overlapping, pause after 3 failed/stopped runs with a loop event to the phone and desktop. Server: `GET /loops`, `POST /agents/{name}/loop/resume|pause|run-now`; phone: `loops` and `pause <name>`; Agents tab: schedule line, next/last run + receipt, Pause/Resume/Run now (mock server speaks the routes). 21 new Python tests in `tests/test_loops.py` + 3 panel logic tests + 1 mock test; full `pytest` passes except the known sandbox-network local-HTTP tests. Docs: `docs/AGENT_PACKS.md`.

## Nov 1, 2026: T3d agents run on laptop, phone or both
Each installed pack has a Runs-on switch (Laptop/Phone/Both) in the Agents tab (`lantern/agentpacks.py`: `surface` in the store wrapper, default laptop, strict validation; `Core.pack_surface`/`set_pack_surface` with no approval; `GET|POST /agents/{name}/surface`; mock server speaks them). The phone gets `agents` (phone-surface packs with Run buttons, 3 per message) and `run <name> key=value` (missing inputs asked in chat; laptop-only refused; unknown names stay ordinary tasks). Cards and results route to the pack's surface: laptop-only stays in the panel, scheduled runs with no channel broadcast when the surface includes the phone, loop-pause notices likewise; Both reaches both and the first tap wins. 10 new Python tests (`tests/test_agentpacks.py` store+routes, `tests/test_channels.py` phone flows, `tests/test_loops.py` scheduled reporting) + 1 panel logic test + mock coverage; full `pytest` passes except the known sandbox-network local-HTTP tests (`test_wire`, mock-server desktop file, one server test). Docs: `docs/AGENT_PACKS.md`.

## Nov 1, 2026: T2 send_to_phone (send a file to the owner's Telegram)
`Channel.send_file` (base raises `NotImplementedError`) + Telegram `sendDocument` over stdlib multipart (`chat_id`, caption cut at 1024, `result.document.file_size` read back; 50 MB refused before sending; WhatsApp stays `NotImplementedError`). New `send_to_phone` tool: policy-only file access, private-zone files refused ("stays on this laptop"), approval bound to name+size+SHA-256 (`Send <path> (<size>) to your phone on Telegram`), file re-read before sending (changed-after-approval refused like `send_email`), `verified` only when the reply is ok with matching bytes, audit row (lane cloud, `api.telegram.org`, name+size, no contents). `Toolbox.phone_sender` wired in `cli.py --serve` to the first channel implementing `send_file` (None → "no phone connected"). One agent-rule line in `BASE_RULES`; new `agents/send-to-phone.lantern-agent.json` example pack (validates). 17 new tests in `tests/test_send_to_phone.py` (fake HTTP only); full `pytest` passes except the known sandbox-network local-HTTP tests (`test_wire`, one desktop, one server) and pre-existing `test_ci_review` git-identity errors, all reproduced on the clean base.

## Nov 1, 2026: T7 import Agent Skills as packs, export A2A agent cards
`lantern/skillimport.py`: hand-written `key: value` front-matter parser (no new dependency) turns a `SKILL.md` file or its folder (at most 64 KB, read only through `lantern.policy`) into a validated pack dict — name slugified, description as goal, Markdown body as instructions cut to 4000 chars with a cut note. Scripts (`*.py`, `*.sh`, `*.js`, `*.ps1`, …) are listed as "not imported (packs never run code)" and never copied. Tools default to the read-only set; `lantern/a2acard.py` builds the Agent Card (A2A spec §4.4.1/§4.4.5 field names; no endpoint URL, no secrets or local paths). `Core.pack_import_skill` (preview, no approval) + `Core.pack_a2a_card`; `POST /agents/import-skill`, `GET /agents/{name}/a2a-card`; install accepts and stores `"source": "imported skill"` as laptop-local wrapper state (like `surface`), never in the shared file, so install → run → export still round-trips byte-identical. Panel: Agents tab "Import a skill…" (Electron file-or-folder picker, older-app markdown file input) with preview, skipped-scripts note and tool checkboxes saving through the normal install approval; each card gets "Share as A2A card" saving `<name>.agent-card.json`. 19 new Python tests (`tests/test_skillimport_a2a.py`) + 1 panel logic test + mock coverage; full `pytest` passes except the known sandbox-network local-HTTP tests (`test_wire` flaky SAM case passes alone and on the file run, one desktop, one server) and pre-existing `test_ci_review` git-identity errors, all reproduced on the clean base. Docs: `docs/AGENT_PACKS.md`, `docs/DECISIONS.md`.

## Nov 1, 2026: T8 GitHub helper pack; packs can declare needed MCP servers
Packs take an optional `needs` list (`lantern/agentpacks.py`: `{mcp, repo, why}`, at most 4, strict validation, secrets scrubbed; `as_dict` omits it when empty so old exports stay byte-identical). `Core.ensure_pack_servers` asks first at install and at run ("This agent needs the GitHub tool server: Add it?" with pinned SHA, license and exact launch command); declining installs the pack but adds nothing, and at run raises before any task is queued. Adding reuses the T6 shape (pinned commit, license shown, red approval, `read_only: []`, verified by reading `mcp.json` back) with a Docker `--read-only` launch for `github/github-mcp-server`. `MCPHub.from_file` expands `${VAR}` refs (token stays `${GITHUB_TOKEN}` on disk, mapped to the server's `GITHUB_PERSONAL_ACCESS_TOKEN`). New `agents/github-helper.lantern-agent.json` example pack (repo + question inputs, 11 read-only `github.*` tools, answer-with-links instructions) and a `doctor` "GitHub helper" check (GITHUB_TOKEN set / not set, never shown). 11 new tests in `tests/test_githubhelper.py` (fake GitHub, no network); full `pytest` passes except the known sandbox-network local-HTTP tests. Docs: `docs/AGENT_PACKS.md`, `docs/DECISIONS.md`.

## Nov 1, 2026: T9 time saved (honest fixed-table estimates, weekly tile)
`lantern/timesaved.py`: fixed per-action minutes (move/organise 0.5 each capped at 20 files per task, form 6, email 3, CAD export 10, link 0.5, LocalSend receive 1) — only `verified: true` counts, never a model. `Memory.finish_task` stores the estimate in a new `time_saved` table (same `CREATE TABLE IF NOT EXISTS` migration pattern; cleared by forget_task/forget_everything); failed/cancelled/stopped tasks store 0. Receipts carry "Estimated time saved: 6 min (form filled)" (phone, CLI, desktop sheet). `GET /stats/time-saved?days=7` (token required) returns the total plus per-day minutes: stored task estimates plus verified `localsend.upload` audit rows. Today tab: fifth "Time saved this week" tile (teal, existing theme) with total and 7-bar mini chart, spoken via aria-label; mock server speaks the route. Docs: `docs/TIME_SAVED.md` ("estimates, not measurements"), `docs/DECISIONS.md` line. 9 new tests in `tests/test_timesaved.py` (+ logic tests, forget-count update); full `pytest` passes except the known sandbox-network local-HTTP tests and pre-existing `test_ci_review` git errors, all reproduced on the clean base.

