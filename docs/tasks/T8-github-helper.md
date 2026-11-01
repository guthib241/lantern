# T8: GitHub helper pack and "packs that need an MCP server"

**Goal:** a ready-made **GitHub helper** agent:
- "summarize my open issues";
- "what changed in my repo this week";
- "list PRs waiting for review".

It is **read-only by default**. Packs can now declare which MCP servers they need, and Lantern offers to add them (with approval).

Read `CLAUDE.md`, `docs/AGENT_PACKS.md` and `lantern/repolearn.py` (T6 already adds MCP servers safely). `python -m pytest -q` must pass.

## Build
1. **Optional pack field `"needs"`:** `[{"mcp": "github", "repo": "github/github-mcp-server", "why": "read your issues and PRs"}]`.
   - When the pack is installed or run and the server isn't in `mcp.json`, show a card: "This agent needs the GitHub tool server: Add it?"
   - **Adding it reuses T6's flow:** pinned commit, license shown, red approval, `read_only` empty.
   - **Never add it silently.**
2. **Credentials:** GitHub's MCP server needs a token. Read it only from the `GITHUB_TOKEN` environment variable / `.env`. Never ask for it in chat, never store it in packs or `mcp.json`. The `mcp.json` env value is `${GITHUB_TOKEN}`, expanded at start (add the expansion if `mcp_client` lacks it, with a test).
3. **The example pack** `agents/github-helper.lantern-agent.json`:
   - inputs: `repo` (owner/name) and `question`;
   - tools: `github.*` read tools only. List the actual read tool names after checking the server's tool list in its README; use the narrowest set;
   - instructions: answer with links, never change anything.
4. **Doctor check:** "GitHub helper: GITHUB_TOKEN set / not set".

## Tests
- `needs` validation;
- a missing server → the add-card appears, and declining runs nothing;
- the token never appears in packs, `mcp.json` or logs;
- `${GITHUB_TOKEN}` expansion;
- the example pack validates and has no write tools.

**Commit:** "T8 GitHub helper pack; packs can declare needed MCP servers".
