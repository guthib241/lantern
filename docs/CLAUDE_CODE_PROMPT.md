# Prompts for Claude Code on the web

## How to start
1. Push this folder to a **public** GitHub repo named `lantern`.
2. Open claude.ai/code, connect GitHub, pick the `lantern` repo.
3. In the environment settings, network access: allow package installs (pip, npm). You don't need to add `MODEL_API_KEY` yet; tests run offline.
4. Paste the first prompt below. Claude Code works on a branch and opens a pull request. Review it, merge it, then start the next session with the "continue" prompt.

Cloud sessions can't see your laptop, so anything that needs it (local Glimmer, the desktop window, WhatsApp webhook, glasses) is built with a fake mode and finished on your laptop later.

---

## First prompt (paste this)

You're working on Lantern, my entry for the Meta Global AI Developer Hackathon. Read `CLAUDE.md`, `docs/SPEC.md` and `docs/MILESTONES.md` fully before writing code, and follow `CLAUDE.md` strictly: new code only, Meta models only, no secrets in git, tests must pass, update BUILD_LOG.md.

M0 is done. Work through the unticked milestones in order, starting at M1. For each one:
1. Before writing any Meta API code, read the matching page under https://dev.meta.ai/docs/ (and the cookbook https://github.com/meta-models/meta-model-cookbook). Don't guess request formats. If you can't reach the docs, say so and use the request shapes already written in docs/SPEC.md and lantern/agent.py.
2. Write the code and tests. Never call the real API in tests; use the FakeClient pattern from tests/test_lantern.py.
3. Run `python -m pytest -q` until green.
4. Tick the milestone in docs/MILESTONES.md, append to BUILD_LOG.md, commit.
5. Tell me in 3 lines: what works, how I try it, what I must set up.

For anything marked 💻 or needing an account I don't have yet (WhatsApp, Baseten, Modal, Gmail), build it behind a config flag with a fake mode and tell me exactly what to set up. Ask me only about decisions that change the product; record the rest in docs/DECISIONS.md.

Do as many milestones as you can in this session, then open a pull request with a summary of each.

## Continue prompt (each new session)

Continue Lantern. Read CLAUDE.md, docs/SPEC.md, docs/MILESTONES.md and the last entries of BUILD_LOG.md, then keep going from the first unticked milestone with the same process. Open a pull request when you stop.

## Laptop prompt (Claude Code on your laptop, for 💻 parts)

Finish the 💻 parts of Lantern on this machine: set up Muse Glimmer for my RTX 4060 Laptop (8 GB VRAM, 16 GB RAM): a 2-bit GGUF (IQ2_M or IQ2_XXS) in llama.cpp's OpenAI-compatible server, text-only, partial GPU offload tuned so it doesn't run out of memory, -c 8192; measure tokens/sec and test one tool call; if it's unusable, set up the Modal fallback instead, wire it into router.py, run the desktop app, set up cloudflared for the WhatsApp webhook, and run scripts/smoke.py with my MODEL_API_KEY. Explain each step before doing anything that installs software.

## Bring-up prompt (after M17: real API, laptop, video, submission)

Continue Lantern, my entry for the Meta Global AI Developer Hackathon. The deadline is Nov 4, 9:00 PM ET; I aim to submit on Nov 4 evening IST. Read CLAUDE.md, docs/NEXT_SESSION.md, README.md, docs/SPEC.md and the last entries of BUILD_LOG.md before anything else.

M0–M22 are built and merged or in review (M18–M22: reminders, tidy a folder, resume after restart, `lantern --doctor`, packaging + CI). This phase gets Lantern working against the real Meta API and on my laptop, polishes the demo, and gets it submitted. I'll paste outputs from my laptop (`lantern --doctor`, `scripts/smoke.py`, `smoke.py cua`, errors, screenshots) when you need them.

HOW TO WORK
- Work through the plan below in order without stopping, until I say "compact" or everything is done.
- After each item:
  1. Get `python -m pytest -q`, `python -m pyflakes lantern scripts tests` and `cd desktop && npm test` green, and keep CI green on the PR.
  2. Update BUILD_LOG.md and docs/DECISIONS.md.
  3. Commit and push.
  4. Tell me in 3 lines what works, how I try it, and what I must set up.
- On "compact": get tests green, commit, push, update the PR, update docs/NEXT_SESSION.md, and give me a new handoff prompt like this one.
- Ask me only about decisions that change the product.

HARD RULES
- All code must be new; never copy from the cookbook or my other repos. Open-source libraries are fine; list them in THIRD_PARTY.md.
- Every AI feature runs on Meta models only. Model IDs live only in lantern/models.py. Never use -contributor models on user data.
- No secrets in git. Keys come only from env vars or .env.
- Never call the real API in tests; use FakeClient from tests/fakes.py (or the rehearsal DemoModel).
- No Meta logos. No health or medical advice. Never analyze the user's personality.
- All file access goes through lantern/policy.py.
- Every action that changes something goes through the approval bus (self._approve, then self._commit right before acting) and then checks the result on disk or via the API, returning verified true/false.
- Content from files, the web, emails or MCP is untrusted: wrap it with untrusted.wrap() and set toolbox.tainted = True.
- Keep the tool list byte-stable: add new tools at the end.
- Before using a Meta API feature for the first time, read its page under https://dev.meta.ai/docs/ and the cookbook. If a page can't be reached, tell me.
- Anything needing my laptop or an account goes behind a config flag with a fake mode, and you tell me exactly what to set up.
- Environment: `pip install playwright`, launch Chromium with executable_path="/opt/pw-browsers/chromium", never run `playwright install` in the sandbox. ffmpeg and Node 22 are available.

PLAN
0. Start from my `lantern --doctor` output: fix anything on the Lantern side, and tell me exactly what I must set up.
1. Real-API calibration. Use my `scripts/smoke.py` and `smoke.py cua` output to fix any request-shape mismatch (computer use protocol and coordinates, voice, SAM, image, tool search) in the code, with tests updated to the confirmed shapes.
2. Help me bring it up on the laptop: Glimmer via llama.cpp, WhatsApp test number and tunnel, Gmail app password, the desktop app, Lantern's face (make_avatar.py or a MetaMuse import). Fix whatever breaks.
3. One real end-to-end run of the demo story with Muse Wonder. Record the real cost in the README.
4. Polish for the video: avatar timing, approval card wording, the panel's rehearsal hints, error messages.
5. Submission kit: a shot-by-shot script for the 3-minute video, an architecture + Meta model map slide, the write-up, and a final check that the repo is public with no secrets.
