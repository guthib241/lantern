# T3d: choose where an agent runs: laptop, phone, or both

**Goal:** in the Agents tab, each agent has a **"Runs on"** switch: **Laptop**, **Phone**, or **Both**. Do this after T3a–T3c are green.

**What that means, honestly:** the work itself always happens on the laptop, because that's where Lantern, the files and FreeCAD live. "Runs on" decides **where you start it from, and where its approvals and results show up**:

| Runs on | Start it from | Approval cards | Result / receipt |
|---|---|---|---|
| Laptop | the panel | the panel | the panel |
| Phone | Telegram/WhatsApp: `run <name> <inputs>` or a button list from `agents` | the phone | the phone |
| Both | either | both (the first answer wins) | both |

- **A setting per installed pack:** `"surface": "laptop" | "phone" | "both"`. It's kept in the store, not in the shared file; the default is `laptop`.
- **Phone keywords** in `ChannelHub`:
  - `agents`: lists the packs whose surface includes the phone, with Run buttons (Telegram inline buttons; at most 3 per message, as today);
  - `run <name> key=value …`: starts one. A missing input → Lantern asks for it in the chat.
- **Scheduled runs (T3c)** report to the pack's surface.
- **Route approvals and results to the surface:** look at how `ChannelHub` already forwards approval cards and `origin[task_id]`.
- **Tests:**
  - a phone-surface pack lists and runs from the fake channel;
  - a laptop-only pack is refused from the phone ("this agent runs on the laptop only; change it in the Agents tab");
  - approvals for a "both" pack reach both, and the first answer wins.
- **Commit:** "T3d agents: run on laptop, phone or both".
