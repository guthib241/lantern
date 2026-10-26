# Instructions for Claude Code (read first, every session)

You are building **Lantern** for the Meta Global AI Developer Hackathon (build window Oct 26 – Nov 4, 2026, submit by Nov 5 7:30 AM IST). The full product spec is `docs/SPEC.md`. The milestone list is `docs/MILESTONES.md`. Work through milestones in order.

## Hard rules (disqualification risks)
1. **All code must be new.** Do not copy code from the author's other repos (PcB-FOrGe, SkillSniff, etc.). Open-source libraries are fine if their license allows it; record each in `docs/THIRD_PARTY.md`.
2. **Every AI feature runs on Meta models.** Muse Spark / Muse Wonder / Muse Image / Muse Voice Transcribe / SAM 3.1 via Meta Model API, or Muse Glimmer / Muse Spark Open locally or on Baseten/Modal/Fireworks. No OpenAI, Anthropic or Google models inside the product. (The OpenAI *Python SDK* is fine: Meta's API is OpenAI-compatible.)
3. **Repo stays public.** Never commit secrets. Keys only from env vars / `.env` (gitignored).
4. **Log the work.** Append to `BUILD_LOG.md` (date + what was built) at the end of each milestone, and commit with a clear message.
5. **Avoid banned project shapes:** no medical/health/nutrition advice (the water reminder is a plain timer, nothing more), not "basic RAG", not a "personality analyzer" (Lantern *has* a personality; it never analyzes the user's).
6. **No Meta logos or branding in the UI.** Our own visual identity; we only name the models we use.

## Engineering rules
- Python 3.11+, package `lantern/`. Desktop UI in `desktop/` (Electron or Tauri, see SPEC).
- Every tool that changes anything: goes through the approval gate, then **verifies on disk / via API** and returns `verified: true/false`.
- All file access goes through `lantern/policy.py` (privacy zones). No other module may open user files directly.
- Tests: `python -m pytest -q` must pass before every commit. Use the scripted `FakeClient` pattern in `tests/` for model calls; never call the real API in tests.
- Model IDs live in one place: `lantern/models.py`. Current IDs: `muse-spark-1.3`, `muse-wonder-1.0`, `muse-image-1.0`, `muse-voice-transcribe-1.0`, `sam-3.1`. Base URL `https://api.meta.ai/v1`, key in `MODEL_API_KEY`. Baseten open model: `meta-models/muse-spark-open-1026`.
- Budget: $150 of API credit. Default to `muse-spark-1.3`; use `muse-wonder-1.0` only for computer use / long tasks. Cache aggressively; log token usage per task in memory.
- Before using a Meta API feature you haven't used yet, read its page under https://dev.meta.ai/docs/ and the cookbook https://github.com/meta-models/meta-model-cookbook. Do not guess request shapes.
- Keep functions small, typed, and documented. Prefer the standard library.

## When unsure
Choose the simpler design that makes the 3-minute demo work reliably, write the decision into `docs/DECISIONS.md`, and continue.
