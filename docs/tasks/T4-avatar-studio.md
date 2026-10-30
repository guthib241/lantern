# T4: Avatar studio (design Lantern's look) and agent faces

**Goal:**
- The user designs Lantern's avatar in the panel by describing it in words. **Muse Image** draws it.
- The avatar can also be a **live skin**, e.g. a clock that shows the real time.
- When agent packs run, Lantern visibly **turns into its agents**: each running agent appears as its own small face next to the main avatar, and clicking one shows what it's doing. This is the "wow" moment for the demo video.

Read `CLAUDE.md` first; its hard rules apply. Before any Muse Image call, read dev.meta.ai/docs (image generation) and reuse the request shape in `lantern/avatar.py`. Don't guess. Branch `claude/freecad-laptop`. `python -m pytest -q` and `npm test` (in `desktop/`) must pass before every commit.

Today:
- `lantern/avatar.py` (`plan`, `generate`, `EXPRESSIONS`) and `scripts/make_avatar.py --brief "..."` already make a 7-expression face with Muse Image (about $0.07), written to `desktop/assets/avatar/` + `manifest.js`;
- `desktop/renderer/avatar.js` shows it, or the built-in glow (`sprite.js`) when there is no art.

## Part A: design your avatar (back end + panel)
1. `POST /avatar/design {"brief": "...", "style": "3d" | "flat" | "pixel" | "neon"}`:
   - validate the brief: plain text, at most 600 characters, secrets scrubbed;
   - generate the canonical image first, as **a preview only**, and return it with its cost.
2. `POST /avatar/design/{id}/accept`:
   - generates the remaining expressions anchored on the canonical one (as `make_avatar.py` does);
   - writes them to `~/.lantern/avatar/<id>/` (Lantern's own folder, never the repo);
   - sets it as the active avatar.

   It needs **an approval** that shows the estimated cost ("7 images, about $0.07"). Log the cost to memory like other model calls.
3. `GET /avatar/manifest` and `GET /avatar/file/{id}/{expression}` serve the active avatar (auth required, only files inside `~/.lantern/avatar/`, names checked against a regex). `avatar.js` loads the manifest from the server first, then falls back to the repo's `desktop/assets/avatar/manifest.js`, then to the glow.
4. **Panel → Setup → "Avatar":**
   - a text box (with the example prompt below as placeholder) and style chips;
   - **Preview** (one image, a few cents) → **Use this** (approval with the cost);
   - a gallery of past designs to switch back to, which needs no model call.
5. **No Meta logos or branding** in any prompt or output (CLAUDE.md rule 6). Add "no logos, no text, no brand marks" to every image prompt.

Example placeholder brief: *"3D animated movie portrait of a sleek personal AI assistant, bright cyan eyes, confident smile, short white-silver hair with neon cyan highlights, dark gray futuristic jacket, soft rim lighting, minimalist dark studio background, clean profile picture."*

## Part B: live skins (no model, no cost)
- In `avatar.js`, a skin is one of `muse` (images), `glow` (today's default) or **`clock`**.
- **Clock:** an analog clock drawn as our own SVG (like `sprite.js`; no user text inside), showing the real local time, updated every second. Lantern's state tints it:
  - working: the ring pulses;
  - waiting for approval: amber;
  - stopped: grey.
- The skin is chosen under Setup → Avatar and saved with the existing `POST /settings`.
- Keep the skin code small, so more skins (e.g. a weather-free "mood ring") can be added later with one function each.

## Part C: agent faces (Lantern turns into its agents)
- An agent pack (T3) may have an optional face: `"face": {"emoji": "📦"}` or a generated icon.
  - **"Make a face for this agent"** in the Agents tab makes one Muse Image icon, about $0.01, with an approval. It's stored at `~/.lantern/agents/<name>.webp`.
  - The pack file stays data-only: the icon is a separate file, and sharing a pack does **not** include it unless the user chooses to.
- While pack tasks run, the avatar window shows **one small face per running agent**, orbiting or stacked beside the main avatar, each with:
  - a tiny activity dot;
  - **click** → opens that task's timeline in the panel;
  - **right-click** → Stop (the existing `POST /stop` for now; per-task stop if T3 added it).

  When the last one finishes, the faces fold back into the main avatar.
- Use the task events the avatar already receives (`LanternState.reduce`); add the pack name to the `queued` event so the avatar knows which face to show.
- Respect `prefers-reduced-motion`: no orbiting, just a row.

## Tests
- **Python:**
  - brief validation (length, secrets);
  - accept needs an approval;
  - declining writes nothing;
  - file routes refuse `..` and unknown names and need auth;
  - the cost is logged (FakeClient image responses; never the real API).
- **Desktop (`desktop/test`, logic only):**
  - clock-hand angles for given times;
  - state → tint mapping;
  - the running-agents list grows on `queued` with a pack and shrinks on `done`/`stopped`;
  - the manifest fallback order.

## Done when
- both test suites pass;
- `docs/AVATAR.md` updated in plain words;
- one `BUILD_LOG.md` line;
- commits: "T4a avatar studio", "T4b live skins (clock)", "T4c agent faces".
