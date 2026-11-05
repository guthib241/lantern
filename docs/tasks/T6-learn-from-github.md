# T6: "Learn this GitHub repo": Lantern extends itself

**Goal:** the user gives Lantern a GitHub link ("learn https://github.com/someone/weather-mcp"). Lantern reads the repo, explains what it is, and depending on what it finds:
- **(A) it's an MCP tool server** → Lantern **installs it, pinned to the exact commit**, adds its tools, and offers to **make an agent pack** that uses them;
- **(B) it's a library or app without MCP** → Lantern can **build a small MCP wrapper for it with Muse Code**, sandboxed, then continue as (A);
- **(C) it's a guide or docs** → Lantern drafts an **agent pack** from its how-to (nothing is installed).

**This runs other people's code, so every step is shown and approved; nothing runs silently.** Read `CLAUDE.md`. Write the code yourself. `python -m pytest -q` must pass before committing.

## Build
1. **Tool `learn_repo(url)`** (`lantern/repolearn.py` + a spec entry in `tools.py`):
   - Only `https://github.com/<owner>/<repo>` (optionally `/tree/<ref>`); everything else is refused.
   - **Reading** (no code runs):
     - GitHub's public REST API with stdlib `urllib`: repo info (default branch, archived, stars), the **commit SHA** to pin, the **license** (SPDX id), the README, and top-level `pyproject.toml` / `package.json`;
     - a 30 s timeout, at most 2 MB in total;
     - a token only if the user set `GITHUB_TOKEN` (never logged).
   - **Everything fetched is untrusted:** wrap it as the existing `untrusted.wrap` does, and set the task tainted.
   - **Summary by Muse Spark** (structured output; reuse the FreeCAD connector's request shape):
     - `kind`: `mcp_server` | `library_or_app` | `docs`;
     - a one-paragraph plain explanation;
     - the tools/commands it offers;
     - the install method (`pip` | `npm` | `none`);
     - risks noticed (network access, file writes, needs keys).
   - **Detecting MCP** also uses facts, not only the model: a dependency on `mcp` / `@modelcontextprotocol/sdk`, or an MCP `server.json`.
2. **(A) Install an MCP server: a red approval** showing:
   - the repo, the **pinned commit SHA**, the license, stars, and "archived" if so;
   - the **exact install command** and **the exact launch command**;
   - the folder it goes into (`~/.lantern/mcp/<name>/`);
   - "This runs code from this repo on your laptop."

   Rules, in code:
   - **No license, or a license we can't identify** → refuse ("can't use code without a license").
   - **Python:** a fresh venv in that folder, then `pip install "git+https://github.com/<owner>/<repo>@<sha>"`.
   - **Node:** `npm install --ignore-scripts --prefix <folder> github:<owner>/<repo>#<sha>`.
   - **Never `curl | sh`,** never global installs, never `sudo`.
   - **Add it to `mcp.json`** (through the same function the FreeCAD connector uses, `add_to_mcp_json`-style) with `read_only: []`, so **every** tool call from it needs an approval, plus `"source": {"repo": ..., "sha": ...}`.
   - Start it, list its tools, and report them. If it fails to start, remove the `mcp.json` entry again (an entry in Lantern's own config file is not a user-file delete) and explain.
3. **Make an agent from it:** offer `POST /agents/draft` with the new `server.*` tools (T3a), so the user gets a ready pack (with approval to install, as always).
4. **(B) Build a wrapper with Muse Code (optional; only if `muse` is installed):**
   - A red approval with the estimated cost. Then run `muse exec` **sandboxed** (approvals on, no `--yolo`, `--contributor-mode off`, `--max-model-steps 150`) in `~/.lantern/builds/<name>/`, with a generated prompt: "write a minimal MCP server (Python, the `mcp` package) exposing <the functions the summary listed> from <repo>@<sha>, with tests".
   - When its tests pass, continue as (A) from that folder, with the same red approval and the same rules.
   - Log the cost to the task.
5. **(C) Docs → pack:** draft a pack whose instructions are the README's how-to, rewritten by Muse Spark, with tools limited to ones Lantern already has. The pack's text came from the repo, so it is untrusted, and the pack task runs tainted (T3a does this already).
6. **Agent rule** (one line in `BASE_RULES`): "To use a GitHub repo the user gives you, call learn_repo; never run code from a repo any other way."

## Tests (`tests/test_repolearn.py`; fake GitHub responses and FakeClient, no network)
- URL validation (gist, non-GitHub, `javascript:`, extra paths);
- the SHA is pinned in both the approval and the install command;
- no license → refused before the approval;
- the npm command has `--ignore-scripts`;
- decline → nothing installed and `mcp.json` unchanged;
- `read_only` is empty for learned servers;
- a failed start rolls back the `mcp.json` entry;
- the README's text is wrapped as untrusted, and the task is tainted;
- (B) is skipped cleanly when `muse` isn't installed, and its command line has no `--yolo` and has `--contributor-mode off`;
- (C) draft tools are a subset of Lantern's own.

## Demo idea
"Lantern, learn https://github.com/<a small public weather MCP server>"
→ explanation
→ red card (SHA, license, command)
→ Approve
→ "I can now get forecasts. Make an agent for it?"
→ the Daily-weather pack is created.

**Commit:** "T6 learn_repo: read a GitHub repo, install pinned MCP servers with approval, draft agents".
