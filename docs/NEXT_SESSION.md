# Handoff: from "all milestones built" to "submitted"

State (Oct 27, session 4, waiting for the key):
- **M0–M30 are all done** on branch `claude/determined-gates-ubpm6u`, PR https://github.com/guthib241/lantern/pull/2 (supersedes #1).
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
- **Tests:** 390 Python tests plus 26 desktop node tests. They include `desktop/e2e/run.js`, which drives the **real Electron app** against the real server in rehearsal mode (needs `cd desktop && npm install`, plus Xvfb on a headless Linux).
- **CI:** GitHub Actions jobs are refused by the account's Actions or billing settings, not by the code. Run the checks by hand before every push.
- **Visibility:** the user chose to keep the repo private. CLAUDE.md and the hackathon rules ask for a public repo, so check the submission requirements with the user before submitting.
- **Real API:** nothing has been called against the real Meta API yet ($0 of $150). The cloud sandbox's network blocks `api.meta.ai`; the user will provide the key (laptop, or allow the domain and add `MODEL_API_KEY` to this environment).
- **Calibration:** `scripts/smoke.py cua` ends with the exact `LANTERN_CUA_PROTOCOL=… LANTERN_CUA_COORDS=…` line for `.env`.

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
| Scripts | `scripts/smoke.py` (one real call per model; `cua` calibrates computer use), `make_avatar.py`, `import_avatar.py` |

## First thing on the laptop
`pip install -e ".[dev]"`, then `lantern --doctor`. It lists every missing piece with the exact fix. Paste its output into the session.

## Not yet verified against the real API (do these first, with the key)
1. `python scripts/smoke.py`: Spark, Wonder, Image, Voice and SAM each make one call. Fix any request-shape mismatch where it lives (`computer.py`, `voice.py`, `sam.py`, `avatar.py`), never in tests.
2. `python scripts/smoke.py cua`: confirms which computer-use protocol the API accepts (native `computer` tool vs the cookbook's `computer.computer` functions) and whether coordinates are pixels or 0..1000. It ends with the exact `LANTERN_CUA_PROTOCOL=… LANTERN_CUA_COORDS=…` line to put in `.env`.
3. Tool search with deferred MCP tools: run once with a small `~/.lantern/mcp.json`. The shape was taken from search snippets of `/docs/tool-search`, because the page itself was unreachable.
4. One real `--serve` run of the full story with `LANTERN_FORMS_MODEL=muse-wonder-1.0`. Note the real cost from `python -m lantern --usage` and put it in the README's "Cost so far". If a single form fill comes close to the $3 per-task limit, the computer-use loop is wasting steps: fix that rather than raising the limit.

## Next steps (Oct 27 – Nov 4)
- **Bring-up on the laptop** (user + you):
  - the key and smoke checks (above)
  - Glimmer via llama.cpp (`LANTERN_GLIMMER_URL`)
  - WhatsApp test number and tunnel
  - Gmail app password
  - `cd desktop && npm install && npm start`
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
