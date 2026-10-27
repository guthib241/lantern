# Phase 2: the signature — "Lantern runs your tasks like an operating system"

**Pitch for judges:** Lantern runs your tasks like an operating system: the cheapest Meta model that can do each step, a new approach whenever something breaks, never sitting idle while it waits for you, and it compiles what it learns into free, instant, verified programs.

This builds ON TOP of everything already in the repo (agent loop, memory, zones, scrubber, audit, approvals, receipts, undo, budget, server, forms, channels). Reuse those modules; do not rewrite them. Where these specs conflict with existing code, adapt the spec to the code and note it in docs/DECISIONS.md.

Priority tiers (never start a lower tier while a higher one is broken):
- **Tier A — must ship:** S0 security backbone (with S1–S4), S1 scheduler, S2 resilience, S3 model ladder, S4 self-compiling routines, S5 demo portal, S8 teach once by demonstration.
- **Tier B — strong extras:** S9 glasses → laptop handoff with S10 photo-to-scan, S11 Sleep (nightly check and repair), S12 "Do this Reel", S6 Lantern Network.
- **Tier C — only if time:** S16 Teaching Mode, S7 Decoy Screens, S13 Lend your Lantern, S14 Wonder teaches Glimmer, S15 small extras.

---

## S0. Security backbone (applies to every feature below; build alongside S1–S4)
Based on published designs (CaMeL: arxiv.org/abs/2503.18813, FIDES: arxiv.org/abs/2505.23643). Verify any paper figures before quoting them; never quote numbers about Lantern that we didn't measure ourselves.

1. **Planner / Reader split.** The Planner (Muse Spark, higher rung) receives only trusted input: the user's request, memory, tool schemas, and *typed* results. It never reads raw untrusted content (web pages, emails, files from others, screenshots, OCR). The Reader (Muse Spark low, or Glimmer for private content) reads untrusted content but has **no tools**; it returns only schema-constrained output (structured output). Prefer low-capacity types: bool, enum, number, date, or a value stored under an opaque handle (`$doc_3.invoice_total`) that the Planner can pass to tools without seeing.
2. **Taint labels.** Every value carries `{integrity: trusted|untrusted, confidentiality: public|private|secret, source}`. Labels join when values combine. A tool argument that is untrusted cannot choose *which* action runs or *who* receives data; red-tier tools refuse untrusted-integrity recipients/targets unless the user approves the exact value. Secret values never reach any cloud rung.
3. **Risk tiers** on every tool: green (read, auto), yellow (reversible local change, one-tap OK, auto-OK only if all args trusted), red (send, submit, pay, install, share; full approval showing exact payload, bound to its hash).
4. **Compiled routines are fixed plans.** A routine's control flow is fixed at compile time (S4), so screen content cannot add actions. Branches may only depend on Reader verdicts (bool/enum). Before each click, cross-check the target against the Windows UI Automation tree / DOM (role, name, enabled, not inside an ad/iframe from another origin); mismatch → stop that branch and go to recovery (S2). Document this as "injection-resistant by construction, with branch-steering checks" — not "injection-proof".
5. **Measure, don't claim.** `scripts/eval_privacy.py`: run the S5 demo-portal tasks N times (e.g. 5 each) with three screenshot modes — raw, masked (black boxes), decoy (S7) — and record success rate, steps, cost, and whether any real private value appeared in any outgoing payload (checked against the audit log). Put the real table in the README. Use `muse-spark-1.3` for most runs to save credit; a few with `muse-wonder-1.0`.
6. **Injection test suite:** demo files, emails and portal pages with hidden instructions (white text, HTML comments, zero-width chars, fake "system" banners). Tests assert no red-tier action happens and the Planner never received the raw text.

## S1. Task scheduler ("never waits idle")
Module: `lantern/scheduler.py`

- Task states: `queued → running → parked(waiting_user | waiting_approval | backoff) → running → done | failed | cancelled`. Persist state, the current step, and everything needed to resume in SQLite (extend memory.py). A killed process resumes all non-terminal tasks on start.
- An asyncio worker pool (default 3 concurrent tasks; configurable). Each task is a coroutine that `await`s its own blockers.
- New tool for the agent: `ask_user(question, options?)`. It never blocks the worker: it parks the task, sends the question through the channel layer (WhatsApp/Telegram/desktop), and the worker immediately picks the next queued task. When the answer arrives, the task is re-queued with the answer injected and continues from the same step (`previous_response_id` chain kept, or replayed from memory if expired).
- Approvals use the same parking path.
- Questions are batched: if 3 tasks are waiting on the user, send one message listing them, with buttons.
- Fairness: round-robin; a task in backoff never blocks others.
- Events on the websocket: `task_parked`, `task_resumed`, with the reason. The avatar shows "juggling" when >1 task is active.

Tests: two fake tasks where task A asks a question; assert task B finishes while A is parked; answer arrives; A finishes; kill-and-restart mid-task resumes correctly.

## S2. Resilience engine ("tries a different way")
Module: `lantern/recovery.py`

On any failed step, classify the failure, then choose a strategy:

| Failure class | Detect by | Strategy |
|---|---|---|
| transient | timeouts, 429, 5xx, connection reset | exponential backoff with jitter (park as `backoff`, other tasks run) |
| tool_error | ToolError from our tools | feed the error back; model must choose a different approach; same tool+args twice in a row is refused |
| approach_failed | postcondition / verify false | next approach in the step's approach list (e.g. UIA selector → keyboard shortcut → computer use) |
| model_confused | invalid JSON args twice, loops (same action 3x), no progress in N steps | escalate one ladder rung (S3) |
| needs_human | missing info, CAPTCHA, password/OTP/card fields, ambiguity | `ask_user` → park (S1) |
| impossible | all approaches and rungs exhausted, or budget hit | fail with a clear report of everything tried |

- Every attempt is logged as a step with `attempt`, `strategy`, `rung`, `cost`, `result`, so the receipt shows the full recovery story.
- Hard limits: max attempts per step, max total cost per task, never retry a non-idempotent action (send, submit, move) unless verification proves it did not happen.

Tests: scripted failures of each class; assert the right strategy; assert a send is never duplicated.

## S3. Model ladder and learning router ("right model for each job")
Module: `lantern/router.py` (extend the existing privacy router; privacy rules always win over cost)

Rungs:
0. compiled routine (S4), 1. deterministic code/rules (no model), 2. Muse Glimmer local (private content, classification), 3. `muse-spark-1.3` reasoning effort low, 4. `muse-spark-1.3` effort high, 5. `muse-wonder-1.0` (computer use, long or stuck tasks).

- Each step gets a `step_kind` (e.g. `file_op`, `classify`, `extract`, `plan`, `form_fill`, `gui_action`, `reply`). The router picks the lowest rung whose recorded success rate for that kind is ≥ threshold (default 0.8, with optimistic start), within the task's remaining budget.
- After each step, record (kind, rung, success, cost, latency) in SQLite. Use a simple, explainable rule: escalate on failure, try one rung lower after 5 straight successes. No black box.
- Private-zone content can only use rungs 0–2 (never escalate private data to the cloud; ask the user or fail instead).
- `lantern --router` prints the table: kind × rung → success %, avg cost, avg time.
- Every step event carries a model badge (rung + model name) for the UI.

Tests: forced failures cause escalation; repeated success causes de-escalation; private content never reaches rung ≥3.

## S4. Self-compiling routines ("Lantern turns itself into software")
Module: `lantern/compiler.py`, routines in `~/.lantern/routines/<name>/v<N>/`

1. After a task finishes `done` and verified, offer: "Want me to learn this?" (or auto for repeated goals).
2. Muse Spark (structured output) turns the step log into a **Routine Contract**: name, typed params (private values as `{{vault.x}}` placeholders, never literals), steps, and a postcondition per step plus a final success check.
3. Muse Spark then writes `routine.py` implementing the contract using ONLY a small allowlisted Lantern runtime API (`files.*`, `uia.find/click/type`, `browser.*` via Playwright, `vault.get`, `check.*`). Static check rejects any other import, `eval`, `exec`, network calls, subprocess.
4. Verify: run the routine in dry-run mode against the recorded run's start state (or the demo portal) and require every postcondition to pass. Only then mark it `trusted`.
5. Next time the goal matches (Muse Spark low-effort or embedding match picks routine + params), run rung 0: zero model calls. If a step's postcondition fails, hand that single step to the S2/S3 recovery path, then recompile and re-verify → `v<N+1>`, log "healed".
6. Receipt per run: model calls, cost, time, versions. `lantern --routines` lists them with run count, total saved cost, last healed.

Demo graph (desktop UI): per routine, cost and time per run going to ~0, with the heal event marked.

Tests: compile a fake successful run; static checker blocks a forbidden import; dry-run pass/fail; a broken step triggers heal + new version.

## S5. Demo portal
`demo/portal/`: a small local web app ("City Permit Portal") with a multi-page form and file upload, plus a switch that changes labels and layout, to show healing live. Seeded fake data only.

## S6 (if time). Lantern Network
Two Lantern instances exchange signed requests (Ed25519 keys, contact allowlist) over the channel layer: ask for a file, share a routine. Owner rules per contact; every request needs owner approval; content is scrubbed; receipts on both sides. Natural-language rules → Cedar policies (`cedarpy`) checked by the engine, with Spark-generated allow/deny examples shown to the owner.

## S7 (if time). Decoy screens
Before a screenshot leaves the device, private values found via UIA/OCR/scrubber are repainted with consistent fake values in matching font/size; the cloud model types fakes; the local actuator swaps real values in at keystroke time and refuses any typed value not in the decoy map. Split-screen "what you see / what the cloud saw" in the UI.

---

## S8. Teach once by demonstration (Tier A)
Module: `lantern/recorder.py`. The user does a task once while Lantern records; S4 then compiles it like any other successful run.
- Capture per input event (`pynput` mouse/keyboard hooks; on Windows also the UI Automation element under the cursor via `uiautomation`/`pywinauto`: AutomationId, Name, ControlType, IsPassword, bounding rect, ancestor path) plus a screenshot. Optional narration through Muse Voice Transcribe ("this field is my passport number").
- Typed values stay local. Values in password fields or matching scrubber patterns become `{{vault.x}}` placeholders before anything is sent; screenshots are scrubbed/masked locally before going to Muse Spark.
- Muse Spark (structured output) turns events + masked keyframes + narration into the Routine Contract (same format as S4), then S4 compiles and verifies it with a dry run while the user watches.
- In the cloud sandbox, test with recorded event fixtures; real hooks are 💻 laptop-only behind a flag.

## S9. Glasses → laptop handoff (Tier B)
Ray-Ban Meta glasses: "Hey Meta, send a photo to Lantern on WhatsApp" (+ voice note). Lantern: S10 cleans the photo → Muse Spark extracts fields (structured output) → matches a trusted routine (e.g. permit renewal) → approval on WhatsApp bound to payload hash → runs the routine on the laptop with the scan attached → replies with the receipt as a short message the glasses read aloud. No glasses SDK; WhatsApp only (Telegram fallback).

## S10. Photo to clean scan (Tier B)
SAM 3.1 with text prompts ("document", "paper", "letter") → largest mask → perspective-correct and crop locally (Pillow/OpenCV) → clean PNG + PDF saved to a writable zone with a sensible name → routines/forms can attach it to file-upload fields (with approval). Read the SAM docs for the output format; parse it carefully and test on fixture images.

## S11. Sleep: nightly check, repair and consolidation (Tier B)
`lantern --sleep` (and a scheduled nightly run):
- Shadow-replay each trusted routine on read-only steps only, stopping before any send/submit/move; check postconditions to catch UI drift early and heal (S4) before the user needs it.
- Merge near-duplicate routines into one parameterized routine (Muse Spark) and suggest new routines from repeated goals in memory.
- Morning digest from the avatar: "Your portal moved the Upload button; I fixed the routine and re-checked it." Delivered on desktop and the phone channel.

## S12. "Do this Reel" (Tier B)
User shares a how-to video (Instagram Reel downloaded/forwarded, or any video file) to Lantern. Muse Spark video understanding → step list (structured output) → safety review (refuse anything touching hidden/private zones, security settings, installs from unknown sources unless approved) → user reviews steps → execution through the normal agent with the ladder, approvals and verification → if it worked, offer to compile it (S4). If a step is unclear, say "I couldn't follow step N" and ask; never guess. Lantern does not scrape Instagram; the user shares the video.

## S13. Lend your Lantern (Tier C)
A family member gets a time-boxed, scoped grant on your Lantern ("may fill forms and download bills for 1 hour; never the Bank folder"). Built on S6 keys and rules. Every action needs the owner's approval on WhatsApp; Hindi/Tamil voice notes through Muse Voice Transcribe; full receipt to the owner. Grants expire automatically and can be revoked instantly.

## S14. Wonder teaches Glimmer (Tier C, stretch)
Export successful step traces (text only, private values already placeholders) as a fine-tuning set. LoRA-tune Muse Glimmer on Fireworks or Modal credits for one narrow job: choosing the next step / routine for a goal. Report before/after accuracy on a held-out set. Be honest in the README that this is a small experiment, not a general improvement.

## S15. Small extras (Tier C)
- Resume PDF → forms profile vault (structured output, user confirms; never used for screening anyone).
- Meeting recording → tasks and reminders (Voice Transcribe with speaker labels).
- Live talk to the avatar on the laptop (realtime Voice Transcribe).
- Avatar reactions made with Muse Image edits (e.g. "celebrating" after a heal).

## S16. Teaching Mode (Tier C, first in Tier C)
A mode of Lantern, never the main pitch. Must NOT look like a "simple AI for education chatbot" (banned category): its value is the verified live board, voice interruption, and checking the learner's handwritten work.

- **Board** (desktop app, `desktop/board/`): full-screen whiteboard. Lesson = a JSON timeline of board actions: `write_math` (LaTeX rendered with KaTeX, revealed stroke-by-stroke/left-to-right), `write_text`, `draw` (line, arrow, circle, axes, simple plot from points), `highlight`, `erase`, `pause`. Each action carries a `say` line spoken in sync.
- **Lesson planner:** Muse Spark (structured output) produces the timeline from the user's request, optionally from their own notes/PDF (through privacy zones; private content goes through the private lane only).
- **Verified math:** every equation step goes through SymPy before it is shown. Each step declares the transformation (e.g. "divide both sides by 2"); the engine checks the new line is equivalent to the previous one (`simplify(lhs1-rhs1 - k*(lhs0-rhs0))` style checks, or `solve` comparison). If a step fails verification, send the error back to Muse Spark to fix it; never display an unverified step. Show a small ✓ next to each verified line.
- **Voice out:** use the OS/browser speech synthesis (Windows SAPI or `speechSynthesis` in Electron). This is not a Meta model; never claim it is. Voice in: Muse Voice Transcribe realtime (push-to-talk is fine) so the user can interrupt: pause the timeline, answer about the current step, resume.
- **Check my work:** user photo of handwritten working (webcam/phone/glasses). Muse Spark image understanding transcribes each line to LaTeX; SymPy finds the first non-equivalent line; SAM 3.1 (text prompt for that line's content or a box from Spark) locates it and Lantern draws a circle on the photo with a short explanation. Be honest when handwriting is unreadable.
- Subjects: start with algebra/calculus (verifiable); other subjects use the board without the ✓ and say so.
- Tests: timeline schema validation, SymPy verifier catches a wrong step, fake-model lesson renders in the desktop e2e.

## Demo script (3 min) — replaces the old one
- 0:00 Hook: queue 3 tasks from the phone at once ("fill the permit form", "send Rahul the slides", "tidy Downloads"). Avatar: juggling.
- 0:25 One task needs info → question arrives on WhatsApp; meanwhile the other two finish (show parked/resumed). Answer it → it continues.
- 0:55 Live model badges: rule → Glimmer (private) → Spark low → Spark high → Wonder, with cost per step. Router table.
- 1:25 A step fails → "trying a different way" → recovers. Receipt shows every attempt.
- 1:50 "Learn this?" → compiled routine. Run again: 0 model calls, $0.00, 4 s.
- 2:05 (if S9 is done) Glasses photo of a paper letter → laptop runs the routine with the scan attached.
- 2:20 Flip the portal layout → step fails → heals one step → v2 → back to $0. (Or show the Sleep morning digest that already fixed it.)
- 2:45 Pitch line + architecture slide with every Meta model and its rung.

## Cut list
Cut from the bottom up: S15, S14, S13, S7, S16, S6, S12, S11, S9/S10, then the router's learning (keep fixed rules per step kind), then question batching. Never cut S1, S2, S4, S8.
