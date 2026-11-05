# T7: Import community skills as packs; export a pack as an A2A Agent Card

**Goal:**
- **Import:** a `SKILL.md` (the Agent Skills folder format) can be imported as a Lantern agent pack in one click, so Lantern gets the community's agents.
- **Export:** a pack can be exported as an **A2A Agent Card** (`agent-card.json`), so other agent tools can discover it.

Read `CLAUDE.md` and `docs/AGENT_PACKS.md`. Write the code yourself from the format descriptions; copy no code. `python -m pytest -q` and `npm test` (in `desktop/`) must pass.

## Import (`lantern/skillimport.py`)
- **Input:** a `SKILL.md` file or a folder that contains one, at most 64 KB.
  - Parse the YAML-like front matter (`name`, `description`) with a small hand-written parser for `key: value` lines only; no new dependency.
  - The Markdown body becomes the pack's `instructions`, cut to 4000 characters with a note that it was cut.
- **Never import scripts or other files.**
  - If the folder has scripts (`*.py`, `*.sh`, `*.js`, `*.ps1`…), list them in the import approval as "not imported (packs never run code)".
- **Tools:** the pack's `tools` default to a safe read-only set (`list_folders`, `list_dir`, `search_files`, `read_file`). The approval lets the user add more from Lantern's own tool list (checkboxes in the panel).
- **The result goes through `agentpacks.validate` and the normal install approval.**
  - It's marked `"source": "imported skill"` and **unsigned**.
  - It is always tainted when run (the T3a rule).
- **Panel:** Agents tab → "Import a skill…" (a file/folder picker).
- **Route:** `POST /agents/import-skill`.

## Export (`lantern/a2acard.py`)
- `GET /agents/{name}/a2a-card` returns an Agent Card JSON describing the pack: name, description, version, the author as provider, one `skills` entry per pack (id, name, description, tags = its tool names, examples from its inputs' examples).
- **No endpoint URL is published:** Lantern doesn't serve agents to the network.
- Follow the A2A spec's field names (read https://a2a-protocol.org or the A2A spec page first). If a field is unsure, leave it out rather than guess, and note it in `DECISIONS.md`.
- **Panel:** in the Share menu, add "Share as A2A card".

## Tests
- **Import:**
  - front matter and body are parsed;
  - scripts are listed and never copied;
  - over-size input is refused;
  - an injection-looking body still imports, but the run is tainted;
  - the default tools are read-only.
- **Export:**
  - the card has the required fields;
  - no secrets and no local paths;
  - it round-trips the pack's name and version.

**Commit:** "T7 import Agent Skills as packs, export A2A agent cards".
