# Handoff: from "all milestones built" to "submitted"

## Live session B (Oct 28): real Meta API and Modal, branch `claude/phase2-core` (PR guthib241/lantern#4, never merge)
Spent on the live API on Oct 28: about **$5.7** (this session ~$5.2, the parallel session $0.52); ~$6.6 of $150 in total. Every number below was measured; details in BUILD_LOG and DECISIONS.
- **Environment:** the container has no `MODEL_API_KEY` variable; the proxy injects the real key for api.meta.ai, so use any placeholder (`MODEL_API_KEY=use-network-secret`). dev.meta.ai is reachable (Markdown at `<page>.md`, index at `/llms.txt`). Modal works after `pip install "modal[api-proxy-support]"`.
- **Docs read, shapes fixed:** S12 sends the clip as `input_video` (frames fallback); S9/S16 readers use `text.format`; S16 sends a SAM box prompt. Voice Transcribe: 25 languages incl. Hindi/Tamil, DIARIZATION, realtime needs the key in the WebSocket's first frame (laptop only; refused from the container). Glimmer: Apache 2.0, LoRA documented. Prices in `models.py` match.
- **phase2_live.py:** checks 1–5 passed (parallel session, $0.16). Check 6 (S4) failed, was fixed (repair prompt; local reader "NOT FOUND"; no handle for a not-found answer) and **passed: learn → 0 calls → heal (21 repairs) → 0 calls, $0.10**.
- **First live runs:** S16 lesson and Check my work (parallel session), S12 video, S10/S9 scan + read, S15 resume, S15 meeting → tasks (new), S6/S13 Cedar (failed on `principal is …`, fixed, 6/6).
- **eval_privacy.py:** 27/30, 0 leaks, $1.70; after the fixes (done-gate in code, the user's request reaches the form planner, planted `role=alert` ignored) 10/10 raw, $0.58. **No screenshots were sent in any run** (no computer use on the portal), so masked/decoy are not compared: that needs a page only computer use can fill.
- **S14 (Modal), done Oct 30:** base Glimmer 93/93 on the held-out set; tuned (QLoRA, merged) 90/93 with answers 277 → 15 tokens. Scripts: `s14_export.py`, `s14_weights.py`, `s14_modal.py --step baseline|train|merge|tuned`. Not wired into the ladder (see DECISIONS Oct 30).
- **Done Oct 30:**
  - Muse Image avatar reactions: 11 images, $0.11.
  - The desktop redesign: a Mac window with sidebar and toolbar, approvals as a sheet, and Meetings, Learned and Teaching sections.
  - The Meetings panel.
  - S15 live talk: push to talk through Lantern's `/talk`. It has not been tried against the real realtime endpoint, which needs the laptop.
  - The screenshot-mode eval on a computer-use page (`eval_privacy.py --tasks reimbursement`): raw, masked and decoy each 5/5, 0 leaks. Your values were shown 38 times in raw and 0 in masked or decoy (36 and 32 covered). $0.64.
- **Still open:** try live talk on the laptop (`LANTERN_TALK_TOKEN=bare` if the Bearer form is refused).


## Phase 2, session 6 (Oct 27): S1 scheduler, S2 resilience, S3 model ladder, S4 self-compiling routines, S0 security backbone, S8 teach by demonstration, on branch `claude/phase2-core`
Spec: `docs/PHASE2_SIGNATURE.md`. The branch started from `claude/gallant-albattani-18fmje` (session 5, everything below), plus the parallel session's `demo/portal`, `demo/attacks`, `desktop/board` and `tests/parallel` (PR #3). It goes to main as its own PR. **Do not merge without the user.**
- **S1 scheduler** (`lantern/scheduler.py`, `lantern/taskrun.py`):
  - 3 tasks at once (`LANTERN_WORKERS`), round-robin.
  - A task parks while it waits: for the user (`ask_user`), an approval, a backoff, or the browser.
  - Checkpoints at every step (`memory.task_state`, encrypted). After a restart, unfinished tasks continue by themselves.
  - Questions are batched on the phone ("#12 answer" or option buttons) and shown as question cards on the desktop; the avatar juggles.
  - Only `lantern --serve` turns it on (`Core(workers=cfg.workers)`). `Core()`, the CLI one-shot and rehearsal keep the old one-at-a-time mode.
- **S2 resilience** (`lantern/recovery.py`):
  - The six failure classes, each with its strategy, in code.
  - Every attempt is an `attempt` step, shown on the receipt.
  - A send, submit or move that happened (or may have) is never repeated in the task. It is keyed on its effect and survives pauses and restarts.
- **S3 ladder** (`lantern/ladder.py`):
  - Rungs 0–5 and step kinds. A rung is picked as the lowest one with ≥ 80% recent success (optimistic start), and stays within budget.
  - Up one rung on failure for the rest of the task; the rung below gets another try after 5 straight successes.
  - Private content is capped at rung 2.
  - The agent's turns run on Spark low/high effort or Wonder. Moving to Wonder starts a fresh chain with a recap.
  - A fill that didn't verify moves computer use to Wonder.
  - Events carry `rung` / `rung_badge`; `lantern --router` and `GET /router` show the table.
- **S4 self-compiling routines** (`lantern/compiler.py`, `lantern/routines/`):
  - **Learn.** "Learn" is offered after a verified task, and happens automatically for a repeated goal.
    1. Lantern's own traces become a contract.
    2. Muse Spark names it and may add params, but can't change the plan.
    3. Muse Spark writes `routine.py`, which a whitelist checks; it must make exactly the contract's calls, or Lantern writes the code itself.
    4. It is verified on a sandbox copy of the site (`LANTERN_ROUTINE_SANDBOX`), or dry up to the first submit.
  - **Run again.** The next time the goal matches, it runs at rung 0: one approval, 0 model calls.
  - **Heal.** When the site moves a field or button, one Muse Spark call picks the new target; the run goes on, and v<N+1> is verified and saved.
  - **Where to see it:** `lantern --routines`, `GET /routines`, "learn [N]" on the phone, and the desktop (offer card; Costs → Learned routines with a cost-per-run chart and heal marks).
  - **For the demo:** run a second portal as the sandbox (`python demo/portal/server.py --port 8771`, `LANTERN_ROUTINE_SANDBOX=http://127.0.0.1:8771`). To show the heal, switch the portal's layout with its footer link or `?layout=v2`.
- **S0 security backbone** (`lantern/labels.py`, `lantern/reader.py`, `lantern/security.py`; details in DECISIONS):
  - The planner never reads content from files, pages, emails, commands or other programs: it gets `$doc` handles and asks the tool-less Reader (`ask_reader`) typed questions. Text answers and private answers are handles too.
  - Recipients, links, commands and tool inputs that came from content are flagged on red cards; private values never reach a cloud model (the audited client refuses the request); red is never one tap.
  - `LANTERN_SCREENSHOTS=masked` and `scripts/eval_privacy.py` (raw vs masked on the clean and attack portal). **Run it with the key and put the measured table in the README** (`python scripts/eval_privacy.py --runs 5 --out eval.json`; a few runs with `--forms-model muse-wonder-1.0`). Decoy screens (S7) aren't built.
  - Tests: 17 security, 13 injection (A1-A7, the portal in attack mode in Chromium), 5 eval/masking.
- **S8 teach once by demonstration** (`lantern/recorder.py`):
  - The person does a web task once in Lantern's browser (desktop: Costs → *Show Lantern once*; or `lantern --teach URL TASK`); Lantern records labels and values locally and learns it with S4.
  - Tested end to end on `demo/portal`: taught once, then the same goal runs with 0 model calls.
  - **For the demo:** run the portal and a second portal as the sandbox, then show it once with `LANTERN_FORMS_HEADLESS=0`. Use the visible browser for the teaching; OS-wide hooks for other apps aren't built (see DECISIONS).
- **Tested with scripted models only:**
  - 16 scheduler, 14 recovery and 10 ladder tests;
  - `tests/test_scheduler_portal.py`: three phone tasks at once against `demo/portal` in Chromium. One parks on a question while the other two finish, then all six portal pages are filled and the portal's own record checked. This run found and fixed a form-filler bug: it clicked Back instead of Next.
  - a dry run of the live script, all 6 checks;
  - S4: 34 tests in `tests/test_routines.py`. The full story runs on `demo/portal` in Chromium: the agent fills it; it's learned and verified on a fresh portal; the rerun takes 0 model calls; the layout flips to v2; the routine heals into v2; the next run is back at 0 model calls.
  - 6 desktop tests.
  - Totals after S0: 566 Python tests and 34 desktop tests green, pyflakes clean.
- **Not yet run on the real API.** This session's container had no `MODEL_API_KEY`, and its network policy refused api.meta.ai (session 5's environment had the network secret). Run it where the key works: `python scripts/phase2_live.py` (6 checks, the 6th being S4 on the portal; $0.25 per-task limit, stops starting checks after `--max-usd`, default $1). Put the measured numbers in BUILD_LOG; quote nothing about Lantern that wasn't measured.
- **dev.meta.ai didn't resolve from this container.** Reasoning effort follows the cookbook's reasoning notebook and session 5's live use of `reasoning.effort: low`. `"high"` and chaining across efforts are untested live; check 4 of the live script covers them. How the API reports an expired `previous_response_id` isn't documented; `taskrun.chain_lost` guesses a 400/404 that names the previous response, and check 5 shows whether the chain survives a restart.
- **Known limits, recorded in DECISIONS:**
  - **Fixed since S1:** multi-page portals now work in production (session cookies per task; pages that move on report `advanced`).
  - **Routines can't do these yet:** pages that needed computer use (custom widgets). Web forms and tidy-ups (rules by pattern) compile.
  - **Healing scope:** heals re-target fields and buttons; a changed flow (a new page) is re-learned from the agent's run instead (S5, `tests/test_relearn.py`; demo/portal `--flow b`).
  - **No avatar art for juggling:** there's no Muse Image "juggling" sprite yet. The avatar shows the thinking face with two orbiting lights; `scripts/make_avatar.py` can add one (~$0.01).

**Live session (real Meta API + Modal): start with [`docs/LIVE_SESSION.md`](LIVE_SESSION.md).**

### What Session B must build next (spec order: Tier A; S4 and S0 are done)
1. **Run the measurements with the key:** `scripts/phase2_live.py` (6 checks) and `scripts/eval_privacy.py`. Quote only what they measure. Then try `lantern --teach` on the laptop with a visible browser.
2. **S5 extras: done.** A changed page flow is re-learned from the agent's run; file tasks compile into rules by pattern (`files.sort`).
3. **Tier B** (spec order): S9 glasses → laptop handoff with S10 photo-to-scan are **done** (scripted tests; try it live: send a photo of a paper to the WhatsApp number with a caption that names a learned routine). S11 Sleep (nightly check and repair, morning digest) and S12 "Do this Reel" are **done** too. S6 Lantern Network is **done** as well, so Tier B is complete. Tier C: S16 Teaching Mode is **done** (lesson planner with SymPy-checked steps, the board window in the desktop app, Check my work on a photo of handwritten working; scripted tests). Try it live: `POST /lesson {"request": "solve x^2 - 5x + 6 = 0"}` with the desktop app open, and a photo of working sent with "check my work". S7 Decoy Screens is **done** too (`LANTERN_SCREENSHOTS=decoy`; scripted tests, no live numbers yet). S13 Lend your Lantern is **done** (`POST /lend`; scripted tests). S15's resume → profile is **done**. Not built: S14 (it needs Fireworks/Modal credits and a real training run, which this container can't reach), and S15's meeting recording → tasks, live avatar talk and Muse Image avatar reactions (each needs a live API shape that isn't verified yet).

State (Oct 27, session 5: tested on the live API):
- **Session 5 (live API testing)** continued on branch `claude/gallant-albattani-18fmje` (PR #1's branch, fast-forwarded to everything from PR #2 plus the fixes below). Which PR to keep is the user's call; see "Open questions".
  - Key: a claude.ai **network secret** for `api.meta.ai` (Bearer) injects the real key into every request; Lantern only needs `MODEL_API_KEY` to exist, so `MODEL_API_KEY=set-by-proxy` works in the cloud. Never print or commit the key. (`lantern --doctor` warns the placeholder "looks cut short"; expected in the cloud.)
  - **Verified live:** all five hosted models (`scripts/smoke.py`); computer use on Spark and Wonder; the whole story plus safety beats via `scripts/live_story.py` (voice note from real speech → Muse Voice Transcribe → form filled with Wonder → approvals → email, private lane, project notes with a planted key, torch, widget, reminder from the phone, tidy + undo, receipt, prompt injection ignored, hidden folders refused, recap); the desktop app against a live server (`cd desktop && npm run live`); resume after `kill -9` of the server; spending limit stopping a task; private lane OFF refusing without leaking; Muse Image avatar sheet; SAM 3.1 (5 ducks on the cookbook photo); tool search with a deferred MCP catalog. About **$0.90** in total; numbers in README → Cost so far.
  - **Fixed from live runs:** Wonder rejects the native computer tool (400) → `LANTERN_CUA_PROTOCOL=auto` (native for Spark, function harness for Wonder, `models.CUA_PROTOCOL`); private-lane answers were re-sent in every later briefing → `private_tasks`; widget calls died at 30 s unstreamed → streamed at `reasoning.effort: low` (14 s, ~$0.014); a generated water timer set an "8 glasses" goal and said "stay hydrated" → agent rule + widget rule + checker; `.wav` voice notes converted onto themselves; tiny budget limits printed as "$0.00".
- **M0–M31 are all done**: originally on branch `claude/determined-gates-ubpm6u`, PR https://github.com/guthib241/lantern/pull/2 (supersedes #1).
  - M18–M22, extras: reminders, tidy a folder, resume after restart, `lantern --doctor`, packaging + CI.
  - M23–M28, design pass:
    - an Apple-style panel (light/dark, Today view, approval sheet with a field table)
    - the live activity island with safe inline approve
    - Quick Ask on Ctrl/Cmd+Shift+Space
    - a menu bar icon, badge and notifications
    - the welcome sheet and Preferences
    - the submission kit (`docs/SUBMISSION.md`, `docs/slide/architecture.png`)
  - Since then:
    - **Wire tests** (`tests/test_wire.py` + `tests/fake_meta_api.py`): Lantern through the real OpenAI SDK over HTTP against a local fake of the API.
    - **A full code review with all 10 findings fixed.** The private lane is now **off** (refuses) without `LANTERN_GLIMMER_URL`, and instructions are resent on every turn.
    - **`docs/GLIMMER.md`**, the laptop walkthrough for the private lane.
    - **M29 Undo:** the Undo button, or "undo" typed or sent from the phone. It replays the task's own move log backwards with one approval and no model call.
    - **M30 Spending limits** enforced in code before every model call: $150 total, $3 per task, optional daily; see `.env.example`.
    - **Windows pass:** explicit UTF-8 for all text I/O (enforced by `tests/test_portability.py`), exact newlines, safe console, `.env` in any encoding (PowerShell writes UTF-16), PowerShell quick start, single-instance lock and AppUserModelID in Electron.
    - **Accessibility:** axe-core WCAG 2.1 AA runs inside the e2e (all tabs, light and dark, Quick Ask, island, receipt sheet); 0 violations; text and fill colour tokens are split.
    - **One command:** `cd desktop && npm start` starts and stops `lantern --serve` itself (`npm run rehearsal` for `--demo`).
    - **M31 Task receipts** (`lantern/receipt.py`, `GET /tasks/{id}/receipt`, `lantern --receipt N`, "receipt" on the phone, Receipt sheet on the desktop).
      - Core now logs approval outcomes per task.
      - The private lane's audit rows are now tagged with their task, so "forget task" removes them; before this they were left behind.
      - Approval listeners now hear "decided" before the waiting task wakes. The HTTP decide route returns the decision itself, which fixes a race where it could return "used".
- **Tests:** 412 Python tests (incl. the Electron e2e in rehearsal mode) + 27 desktop node tests, all green at the end of session 5, pyflakes clean. (The Electron binary may need `node node_modules/electron/install.js` twice after `npm install` in the sandbox.)
- **Tests:** about 401 Python tests plus 27 desktop node tests. They include `desktop/e2e/run.js`, which drives the **real Electron app** against the real server in rehearsal mode (needs `cd desktop && npm install`, plus Xvfb on a headless Linux).
- **CI:** GitHub Actions jobs are refused by the account's Actions or billing settings, not by the code. Run the checks by hand before every push.
- **Visibility:** the user chose to keep the repo private. CLAUDE.md and the hackathon rules ask for a public repo, so check the submission requirements with the user before submitting.
- **Real API:** reachable from the cloud sandbox now (network secret). Spent so far: about $0.90 of $150.
- **Calibration:** done; no `.env` line needed (`auto`). `scripts/smoke.py cua` says "nothing" when auto already matches.
- **Known risk (not changed):** agent turns are not streamed. None came near the 30 s upstream cut-off live, but a very long-thinking turn could. Fix recipe in DECISIONS.md (Oct 27).

## Open questions for the user (from session 5)
1. **Private answers and the cloud.** By the spec, the cloud model sees Glimmer's short answer (e.g. the salary) inside that one task; it is no longer re-sent later. Alternative: deliver private-lane answers straight to the user (desktop/phone) and give the cloud model only "answered privately", which fits the pitch "never reads my private folder" better but means the agent can't use the answer in follow-up steps. Ask which they want before changing it.
2. **Which PR to keep:** #1 (this branch, has everything + live fixes) or #2 (needs these commits pushed to `claude/determined-gates-ubpm6u`).
3. **The face:** a Muse Image avatar sheet was generated in session 5 (consistent lantern character, 7 expressions) but not committed; ask whether to use it or their MetaMuse art (`scripts/make_avatar.py` regenerates for ~$0.07).

The remaining work is **laptop-side bring-up with real keys, calibration against the real API, polish, the video and the submission**. Deadline: Nov 4, 9:00 PM ET (Nov 5, 7:30 AM IST); aim to submit Nov 4 evening IST.

## Working rules (unchanged)
- Follow `CLAUDE.md` strictly:
  - new code only
  - Meta models only, with IDs only in `lantern/models.py` and never `-contributor` on user data
  - no secrets in git
  - never call the real API in tests (use `tests/fakes.py` or the rehearsal `DemoModel`)
  - no Meta logos, no health or medical advice, never analyze the user's personality
- All file access goes through `lantern/policy.py`. Every change goes through `self._approve` → `self._commit` right before acting, then is verified (`verified: true/false`). Untrusted content is wrapped with `untrusted.wrap()` and sets `toolbox.tainted = True`. New tools go at the end of `TOOL_SPECS`.
- After each piece of work:
  1. `python -m pytest -q` green, `python -m pyflakes lantern scripts tests` clean, and `cd desktop && npm test`. CI must stay green on the PR.
  2. Update `BUILD_LOG.md` and `docs/DECISIONS.md`.
  3. Commit (with the attribution lines) and push to the session's branch.
  4. Post a 3-line note: what works / how to try / what to set up.
- Before using a Meta API feature for the first time, read its dev.meta.ai page and the cookbook (read only). From the cloud sandbox, dev.meta.ai, developers.facebook.com and core.telegram.org were unreachable; say so when that happens.
- Ask the user only about decisions that change the product. Otherwise pick the simpler option that keeps the demo reliable and record it.

## Architecture (reuse, don't rebuild)
| Area | Files |
|---|---|
| Models, costs | `lantern/models.py` (IDs, prices, badges, `check_model`) |
| Privacy | `policy.py` (zones; the only file I/O), `scrubber.py` (now also scrubs decoded JSON tool outputs; wire ids untouched), `audit.py` (`GuardedClient`, `AuditLog`), `crypto.py` (Fernet, keychain or key file), `untrusted.py` |
| Agent | `agent.py` (Responses loop, `previous_response_id`, events), `tools.py` (`Toolbox`, `TOOL_SPECS`), `memory.py` (encrypted, real forget), `approvals.py` (bus, nonce, hash, expiry) |
| Runtime | `core.py` (worker, events, STOP, retry_task, reminder scheduler, `recover_interrupted` / `resume`, `undo`), `server.py` (FastAPI 127.0.0.1 + token + websocket), `reliability.py` (`RetryingClient` with a `before_call` hook, breaker, `explain`), `budget.py` (spending limits), `cli.py` (`--doctor`, `--resume`, refuses a second server) |
| Features | `forms/` (Playwright driver, computer-use loop with native + function protocols, guards, vault, filler), `channels/` (WhatsApp, Telegram, fake phone, hub), `email_tool.py`, `router.py` (private lane: Glimmer), `envmap.py`, `widgets.py`, `mcp_client.py` + `mcp_server.py`, `sam.py`, `avatar.py`, `voice.py` |
| Extras | `reminders.py` (time parsing, repeats; storage in `memory.py`), `organize_files` in `tools.py` (one plan-bound approval), `undo.py` (plans from the logged reverse moves; `Toolbox.undo_moves` isn't a model tool), `doctor.py` (read-only setup checks) |
| Packaging | `pyproject.toml` (`pip install -e .` → `lantern`), `.github/workflows/ci.yml` |
| Rehearsal | `demo.py` (`DemoModel`, `prepare`, `build`, `run_story`), `demo/home/`, `demo/forms/`, `demo/profile.example.json` |
| Desktop | `desktop/` (Electron 44.5.1; `renderer/lib/*.js` are pure, tested modules; `mock/server.js` for UI-only rehearsal) |
| Scripts | `scripts/smoke.py` (one real call per model; `cua` calibrates computer use), `make_avatar.py`, `import_avatar.py`, `phase2_live.py` (S1–S3 on the live API) |
| Phase 2 | `scheduler.py` (slots, parking, questions, restart), `taskrun.py` (`ResumableAgent`: checkpoints, `ask_user`, rungs per turn), `recovery.py` (failure classes, never-twice guard), `ladder.py` (rungs, kinds, learning table), `compiler.py` (learn, verify, match, run, heal) + `routines/` (`contract.py`, `checker.py`, `runtime.py`, `store.py`, `runner.py`) |

## First thing on the laptop
`pip install -e ".[dev]"`, then `lantern --doctor`. It lists every missing piece with the exact fix. Paste its output into the session.

## Still needs the laptop (can't be verified in the cloud)
1. Muse Glimmer on the RTX 4060 (`docs/GLIMMER.md`), then ask the private-file question with `LANTERN_GLIMMER_URL` set (only the offline stand-in and the "lane off" refusal were tested live).
2. WhatsApp Cloud API test number + cloudflared tunnel; a real voice note from the phone (and from the glasses). Only the simulated phone was tested live; espeak's robot voice heard "Fill" as "kill", so check transcription with a human voice.
3. Gmail app password (SMTP send + IMAP Sent check); only the fake outbox was tested live.
4. The desktop app on Windows (`cd desktop && npm start`), the webcam for the SAM check-in, and a forms run with `LANTERN_FORMS_HEADLESS=0` to film it.

## Next steps (Oct 27 – Nov 4)
- **Bring-up on the laptop** (user + you):
  - the key and smoke checks (above)
  - Glimmer via llama.cpp (`LANTERN_GLIMMER_URL`)
  - WhatsApp test number and tunnel
  - Gmail app password
  - `cd desktop && npm install && npm start` (starts the server too; `npm run rehearsal` for the scripted story)
  - `python -m playwright install chromium`
- **Lantern's face:** `python scripts/make_avatar.py` (~$0.07), or the user exports art and Vibes clips from the MetaMuse app and runs `scripts/import_avatar.py`.
- **Polish for the video:**
  - avatar timing
  - approval card wording
  - a clean `demo/home` on the laptop: copy it into real folders, or record with `--serve --demo` for the rehearsal shots
- **Submission:**
  - the 3-minute video following the README's demo table
  - the architecture + Meta model map slide (the README's mermaid diagram)
  - the write-up
  - make sure `.env` is not committed, and settle the repo's visibility with the user (see State)
- **Open questions from the user, not answered yet:**
  - "they can use api too… a private local [model] … muse or meta app does that for u": Lantern already runs several channels at once and a local private lane. Confirm with the user whether they meant something else (computer use on local Glimmer? positioning?).
  - The user mentioned Muse's voice. No public Muse voice / TTS API was found. If the user shares a doc link, wire it into the desktop bubble and WhatsApp replies.

## Environment notes (cloud sandbox)
- Python 3.13, Node 22 and ffmpeg are available. `pip install playwright`, then launch Chromium with `executable_path="/opt/pw-browsers/chromium"`. **Never run `playwright install` in the sandbox.**
- Tests must stay offline and fast. Browser tests skip cleanly without Chromium, but they run in the cloud.
