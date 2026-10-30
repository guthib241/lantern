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
