# Lantern: feature ideas and order

Deadline: **Nov 4, 9:00 PM ET** (Nov 5, 7:30 AM IST); aim to submit on the evening of Nov 4, IST. Rule for the list: a feature is in only if it can be shown working in the 3-minute video. Everything runs on Meta models (Muse Spark / Wonder / Image / Voice Transcribe / SAM 3.1).

## Building now (task cards in `docs/tasks/`)
| # | Feature | Why people will love it | Status |
|---|---|---|---|
| T1 | **Open on my laptop**: share a link from any phone app → it opens on the laptop | One tap from the couch to the big screen | ✅ built (Muse Code), tested |
| T3a | **Agent packs**: an agent saved as one small file, shareable like an extension | Get a friend's agent without writing prompts | 🔨 Muse Code building |
| T3b | **Agents tab**: run, watch live, stop midway, share, "make an agent from this task" | One place for all your agents | next |
| T4 | **Avatar studio**: describe your Lantern and Muse Image draws it; **clock skin**; **Lantern turns into its agents** while they run | Personal and visual; the "wow" shot in the video | next |
| T3c | **Loop agents**: agents on a schedule (every morning, every 2 h…), with brakes | Lantern works while you don't | after |
| T3d | **Runs on laptop / phone / both** | Start and approve agents from your pocket | after |
| T2 | **Send this file to my phone** (Telegram) | Your laptop's files, anywhere | after (needs Telegram) |

## Strong next ideas (after the hackathon, or if time is left)
- **"Time saved" counter:** each receipt estimates the minutes saved; a weekly total in the panel. People love a number that grows.
- **Agent gallery:** a public list of shared packs, installed with one click. Each one shows the tools it uses and its author, and asks for approval as today.
- **Share a pack by Telegram/QR:** send the `.lantern-agent.json` to a friend; their Lantern installs it after approval. This builds on T2.
- **Voice everywhere:** a voice note on the phone starts any agent (Muse Voice Transcribe already works).
- **Screen cast:** "show this on the TV" presses Windows' built-in Cast (Win + K) for Miracast TVs.
- **Phone app:** a small Android companion that knows what's playing, so "open this on my laptop" works by voice alone, without Share.

## Not doing (and why)
- **Every TV / every OTT app / installing apps on TVs:** there's no test TV, each brand differs, and OTT apps block remote control. It can't be shown reliably by Nov 4.
- **OpenCode** (`install-opencode.sh`): a third-party coding tool configured for the older Muse Spark 1.1. We use Muse Code (Meta's own agent) instead.
- **Health / nutrition advice, personality analysis, "basic RAG":** banned project shapes (CLAUDE.md).

## Research: GitHub and Hugging Face (Oct 30)
These are ideas only: no code is copied (CLAUDE.md rule 1). A library is used only if its license allows it, and it is recorded in `docs/THIRD_PARTY.md`.
- **Agent file formats exist; none is the standard yet.**
  - **A2A Agent Cards** (`agent-card.json`, now under the Linux Foundation): describe an agent's identity and skills.
  - **Agent Skills** (`SKILL.md` folders): instructions plus scripts.
  - **AAIF**: an IETF draft.
  - **Agent Format** (`.agf.yaml`, Apache-2.0).

  **Idea:** "Import a skill" turns a `SKILL.md` into a Lantern pack. Only the instructions are imported, **never its scripts** (packs stay data-only). Export a pack's description as an A2A card, so other tools can discover it. That gives Lantern a large library of community agents on day one.
- **"Agents for GitHub":** GitHub runs an official MCP server, and there is a public MCP Registry. Lantern already loads MCP servers as deferred tools. **Idea:** a "GitHub helper" pack ("summarize my open issues", "what changed in my repo this week") that uses GitHub's MCP server, read-only by default. A pack could also list the MCP servers it needs, and Lantern would offer to add them (with approval).
- **3D avatar:** pixiv's **three-vrm** (MIT) renders `.vrm` 3D characters in a browser/Electron window, and open desktop companions already use it for lip-synced overlays. **Idea for after T4:** a "3D skin" that loads a user's own `.vrm` file (made in VRoid etc.; not an AI feature, so Rule 2 isn't affected). It would add Three.js + three-vrm, recorded in THIRD_PARTY.
- **Hugging Face:** the product may only use Meta models, so non-Meta models there are out. Lantern already uses Muse Glimmer locally for private files. Muse Spark Open weights are reported as released (gated); we use the hosted API, so nothing changes for the hackathon.

## More research (Oct 30, second pass)
- **Meta's own cookbook** (github.com/meta-models/meta-model-cookbook, MIT), the recipes that matter for us:
  - `05_muse_image/03_anchored_generation`: keep one character consistent across a series (avatar expressions, agent faces). `04_image_editing_with_reasoning`: "give my avatar a hat" edits. **Used by T4.**
  - `04_muse_code/06_subagent_fanout`: Muse Code runs several sub-agents in parallel, each in its own git worktree, and the parent can watch, steer or **stop any of them midway**. It's the same idea as Lantern's agents, and a faster way for us to build.
  - `04_muse_code/03_staged_approvals`, `05_immutable_guardrails`, `01_append_only_audit_log`: the same safety ideas Lantern uses (approvals per stage, an audit log); worth citing in the submission.
  - `03_use_cases/11_github_repo_agent`: a GitHub bot on Muse Spark (GitHub Actions) that triages issues and reviews PRs. **Idea:** run it on Lantern's own repo, so Muse Spark reviews each Muse Code change too. It needs `MODEL_API_KEY` as an Actions secret (never in the repo).
  - `03_use_cases/03_visionclaw`: see-and-ask on Ray-Ban Meta glasses. Lantern already reaches glasses through WhatsApp; a natural "next" line for the video.
- **Phone ↔ laptop without the cloud:** LocalSend (MIT, all platforms) and KDE Connect (GPL; ideas only) move files and text over local Wi-Fi with end-to-end encryption. **Idea:** Lantern accepts LocalSend's open protocol, so Share → LocalSend → "Lantern" works at home without Telegram. That is more private, and it runs on iPhone too.
- **What people love in desktop companions** (DeskCat, Open-LLM-VTuber, Mochi…):
  - always-on-top pets;
  - importing your own character/sprites;
  - being "alive" with idle animations even without the model;
  - focus timers;
  - a live 2D/3D talking avatar.

  T4 (studio, clock skin, agent faces) covers the first three.
- **Safety for shared agents:**
  - a 2026 study found about 26% of 42k community "skills" had a vulnerability (often prompt injection);
  - one approval at install can silently grant lasting access (the "consent gap");
  - content scanners miss a lot.

  Lantern already does what's recommended: permissions shown at install, a code-enforced tool allowlist, approvals on **every** change, and untrusted handling. **Next step:** **signed packs.** The author signs with an Ed25519 key (the `cryptography` package is already a dependency), and the panel shows "signed by achan ✓" or "unsigned". A changed pack fails the check.
