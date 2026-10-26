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
