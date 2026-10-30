# T3: Agent packs (shareable agents, like extensions)

**Goal:** a user can save a ready-made agent ("CAD helper", "Tidy my Downloads", "Weekly report mailer") as **one small file**, share it, and install someone else's in one click. People get the same feature without writing the prompt themselves.

In the desktop panel, the user can:
- see installed agents;
- run one, filling in a few blanks instead of writing a prompt;
- watch it work step by step;
- **stop it midway**;
- turn a finished task into a new agent.

Read `CLAUDE.md` first; its hard rules apply. Branch `claude/freecad-laptop`. `python -m pytest -q` must pass before every commit.

## The pack format: data only, never code
One JSON file, `<name>.lantern-agent.json`, at most 32 KB:
```json
{
  "format": "lantern-agent/1",
  "name": "cad-enclosure",
  "title": "Enclosure maker",
  "description": "Makes a 3D-printable box with mounting holes and exports an STL.",
  "author": "achan",
  "version": "1.0.0",
  "inputs": [
    {"key": "size", "question": "Outer size (L×W×H mm)?", "example": "40×20×15"},
    {"key": "holes", "question": "Mounting screws?", "example": "two M3 in the floor"}
  ],
  "goal": "Make an enclosure {size} mm with 2 mm walls and {holes}, check it, and export an STL into Documents\\CAD.",
  "instructions": "Use the freecad tools. Call freecad.check_model before saying it is done.",
  "tools": ["freecad.*", "list_folders", "search_files"]
}
```

Validation, in `lantern/agentpacks.py` (new):
- Unknown keys are refused.
- `name`: `^[a-z0-9][a-z0-9-]{0,40}$`.
- `title`, `description`, `author`: plain text, at most 200 characters each.
- `instructions`: at most 4000 characters. `goal`: at most 1000 characters, and every `{key}` in it must be an input.
- At most 6 inputs. Values filled in at run time are plain text, at most 500 characters, with `{` and `}` removed.
- `tools`: each one is a name in `tools.TOOL_NAMES`, or `server.*` / `server.tool` for MCP tools. `finish` is always allowed. An empty list is refused.
- **No `model` field.** A pack can never pick a model (no `-contributor` models, no surprise costs).
- **Secrets:** run the secret scrubber over every text field (see how `SCRUBBER` is used in `lantern/channels/hub.py`). Refuse the pack if anything is found.

## Back end
1. **`lantern/agentpacks.py`:**
   - `validate(data: dict) -> Pack` raises `PackError` with a clear message.
   - `Pack.render_goal(inputs: dict) -> str`.
   - `PackStore(dir)` stores packs in `~/.lantern/agents/` (Lantern's own folder, like `widgets.py`):
     - `list()`, `get(name)`;
     - `install(data)`: validate, then write `<name>.json`. It refuses to overwrite a different author's pack with the same name unless `replace=True`;
     - `disable(name)` / `enable(name)`: a flag in the file. **No deletes** (CLAUDE.md);
     - `export(name) -> bytes`: the exact file, to share.
2. **Install needs approval.** The card shows:
   - the title, author and version;
   - **every tool the pack may use**;
   - the full instructions text.

   Bind it to the SHA-256 of the file's bytes, exactly like other approvals (`Toolbox._approve` / `_commit`, or the `BusGate` in Core).
3. **Running a pack**: `Core.submit(goal, pack=name, inputs=...)`.
   - The task's goal is `[agent:<name>] ` + the rendered goal.
   - The pack's instructions go at the **end** of the agent's instructions, after "Now:", under the heading `# Agent pack "<title>" by <author> (installed by the user; Lantern's rules above always win)`. The prefix stays byte-stable for prompt caching.
   - **Tools are enforced in code:** `Toolbox.allowed_tools` (a set or `None`) for the task; `Toolbox.call` refuses any other tool with a `ToolError` ("this agent pack may not use X"). Patterns like `freecad.*` match MCP tool names.
   - **Set `toolbox.tainted = True`** at the start of a pack task. A shared pack was written by someone else, so Lantern's existing protections for untrusted content apply. For example, email addresses that appear in the pack are treated as untrusted.
   - All approvals still happen as usual. A pack never skips one.
4. **Create an agent from a finished task**: `POST /agents/draft {"task_id": N}`.
   - Muse Spark (`models.SPARK`, structured output: read dev.meta.ai/docs structured output first) drafts a pack from the task's goal and the tools it actually used, turning concrete values into `inputs`.
   - The draft is validated and returned to the panel for the user to edit. Saving it goes through install (with approval).
   - Log its cost to the task like other model calls.
5. **Server routes** (`lantern/server.py`, all with `Depends(auth)`):
   - `GET /agents`;
   - `GET /agents/{name}`;
   - `POST /agents/install` (body: the pack JSON);
   - `POST /agents/{name}/run` (body: `{"inputs": {...}}`) → `{"task_id": N}`;
   - `POST /agents/{name}/disable`, `POST /agents/{name}/enable`;
   - `GET /agents/{name}/export` (the file, as a download);
   - `POST /agents/draft`.
   - Stopping uses the existing `POST /stop`. Watching uses the existing task events and `GET /tasks/{id}`.
6. **Three example packs** in `agents/` (repo root), tested to validate:
   - `cad-enclosure` (the one above);
   - `tidy-downloads` (`organize_files` and friends);
   - `send-to-phone` (`search_files`, `send_to_phone`, which T2 adds). If T2 isn't merged yet, skip this one.

## Desktop (part 2: a separate commit after the back end is green)
New **Agents** tab in `desktop/renderer/panel.*`, matching the panel's existing look.
- **Cards** for installed packs: title, description, author, tool chips, and **Run** / **Share** / **Disable** buttons.
- **Run:** a small form with one field per input (showing its example) → starts the task → shows the live timeline (reuse the Today tab's task view) with a big **Stop** button.
- **Install:** a "Add agent…" button: a file picker for `.lantern-agent.json`, or drag and drop. It shows the install approval.
- **Share:** saves the exact file through Electron's save dialog (main process, IPC like the existing ones in `preload.js`).
- **Make an agent from this:** on a finished task in Today → opens the draft for editing → install.
- Add the panel tests in `desktop/test` (logic only, as the existing tests do); `npm test` must pass.

## Tests (`tests/test_agentpacks.py`; FakeClient, never the real API)
- **Validation:** good pack ok; unknown key; bad name; a `model` field; a tool that doesn't exist; `{x}` in goal without input `x`; over 32 KB; an API-key-looking string in instructions → refused.
- **Install:** approval card lists the tools; decline writes nothing; approve writes the file; a different author with the same name needs `replace=True`.
- **Run:**
  - the goal is rendered;
  - the instructions are added after "Now:" (and the start of the instructions is byte-identical to a normal task);
  - a tool outside the pack's list is refused;
  - `freecad.*` allows `freecad.box`;
  - `tainted` is True;
  - STOP cancels a running pack task.
- **Draft:** a FakeClient-scripted structured answer becomes a valid pack.
- **Server routes:** auth is required; install → run → export round-trips the same bytes.

## Done when
- `python -m pytest -q` and `npm test` (in `desktop/`) pass;
- `docs/AGENT_PACKS.md` explains the format and the safety rules in plain words;
- one line in `BUILD_LOG.md` and a `DECISIONS.md` entry;
- two commits: "T3a agent packs: format, store, run with tool allowlist, routes" and "T3b desktop Agents tab".
