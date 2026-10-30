# T2: "Send this file to my phone" (laptop → Telegram)

**Goal:** from the phone or the desktop, the owner says "send me the enclosure STL" / "send report.pdf to my phone". Lantern finds the file, shows a **red** approval naming the exact file, and sends it to the owner's Telegram chat as a document. Email already works through `send_email`; this adds Telegram.

Read `CLAUDE.md` first; its hard rules apply. Work on branch `claude/freecad-laptop`. `python -m pytest -q` must pass before you commit.

## What to build
1. **`Channel.send_file(data: bytes, filename: str, caption: str = "") -> dict`** in `lantern/channels/base.py`:
   - The base class raises `NotImplementedError`.
   - Returns `{"ok": bool, "bytes": int}`, where `bytes` is the size the service reports back.
2. **Telegram implementation** in `lantern/channels/telegram.py`: Bot API `sendDocument`, as `multipart/form-data`.
   - Fields: `chat_id` = owner, `caption` (≤ 1024 chars), and `document` (the file).
   - Build the multipart body with the standard library (a random boundary). Send it through the existing `self.http`, so tests can fake it.
   - Read `result.document.file_size` from the reply.
   - Telegram bots can send at most **50 MB**; refuse larger files before sending.
   - WhatsApp: leave `NotImplementedError` and say "Telegram only for now" in the tool result.
3. **Tool `send_to_phone`** in `lantern/tools.py`. Parameters: `path` (string, required), `caption` (string, optional).
   - Read the file **only through `self.policy`** (`check_visible`, `stat`, `read_bytes`); never `open()` (CLAUDE.md: all file access goes through `lantern/policy.py`).
   - **Private-zone files are refused** ("stays on this laptop"), hidden zones too.
   - Payload: `{"file": name, "bytes": size, "sha256": hash, "to": "Telegram (owner)"}`. A **red** approval: `"Send <full path> (<size>) to your phone on Telegram"`, bound to that payload through `_approve` / `_commit`.
   - Re-read the file right before sending. If its hash changed since the approval, refuse; `send_email` does the same, so copy that pattern from `lantern/email_tool.py`.
   - After sending: `verified = reply ok and reply bytes == size`.
   - Record in the audit log what left the laptop: lane "cloud", destination "api.telegram.org", the file name and size, no contents. See how `ChannelHub._deliver` records sends.
   - The toolbox needs a way to reach the channel: add `self.phone_sender = None` to `Toolbox.__init__` (a callable `(data, filename, caption) -> dict`). Wire it where the `ChannelHub` is created (`lantern/cli.py` `--serve` path) to the first channel that implements `send_file`. When it's `None`, the tool returns `{"done": False, "reason": "no phone connected (set up Telegram, see README)"}`.
4. **Agent rule**, one line in `BASE_RULES`: `- "Send X to my phone": find the file, then call send_to_phone once. Never send private-zone files.`

## Tests (`tests/test_send_to_phone.py`; fake HTTP, never the real Telegram or Meta API)
- Telegram `send_file` builds a multipart request with chat_id, caption and the exact bytes, against a fake `http`.
- Tool:
  - approve → sent once, `verified=True`;
  - decline → nothing sent;
  - private-zone file → refused before any approval card;
  - a file over 50 MB (fake `stat`) → refused;
  - the file changed after approval → refused, nothing sent;
  - no phone connected → a clear reason.
- The audit log has one cloud entry for the send, with name and size, and no file bytes.
- Agent: a FakeClient script for `[telegram] send me Documents/CAD/enclosure.stl` calls `send_to_phone` with that path.

## Done when
- `python -m pytest -q` passes;
- one line in `BUILD_LOG.md`;
- a commit "T2 send_to_phone: send a file to the owner's Telegram after a red approval".

**Manual check on the laptop:** with Telegram on, from the phone: "send me the enclosure STL" → a red card on the phone → Approve → the STL arrives in the chat and opens in a 3D viewer app.
