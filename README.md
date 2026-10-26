# 🏮 Lantern

A private AI companion that lives on your computer, remembers what it's doing and what it has done, and gets real work done on your files. Built on Meta's Muse Spark for the Meta Global AI Developer Hackathon (Oct 26 – Nov 4, 2026).

## What works today (Day 1)

- **Agent loop on Muse Spark 1.3** (Responses API, tool calling, `previous_response_id` so reasoning carries across steps)
- **Memory** in a local SQLite file:
  - every task with status (`active` / `done` / `failed`) and a one-line summary of what it actually did
  - a step-by-step timeline of each task (plan, tool calls, results, reply)
  - lasting facts about you ("manager email", "tax folder"), learned during tasks
  - all of it is fed back to the model on every task, so Lantern knows what it already did
- **File tools**: list, search by name or content, read, write, move
- **Safety**: locked to folders you allow (no `../` or symlink escapes), asks before any change, never deletes, verifies every change on disk
- **Personalities**: `smug` (default), `sarcastic`, `helpful`

## Setup

```bash
pip install -r requirements.txt
export MODEL_API_KEY="your-key-from-dev.meta.ai"      # Windows: $env:MODEL_API_KEY="..."
export LANTERN_ROOTS="$HOME/Documents:$HOME/Downloads" # folders Lantern may touch (Windows: use ; between folders)
```

## Use

```bash
python -m lantern                                   # chat mode
python -m lantern "find my march invoice and move it to Documents/Tax"
python -m lantern --tasks                           # what Lantern has done
python -m lantern --task 3                          # full timeline of task 3
python -m lantern --memory                          # what it remembers about you
python -m lantern --persona sarcastic
```

## Forms (computer use)

Lantern fills web forms in its own browser (a separate Chromium profile with no saved passwords):

```bash
pip install playwright && python -m playwright install chromium   # once, on your laptop
python -m lantern --profile-set "full name" "Asha Rao"              # the local profile vault
python -m lantern "fill the hackathon reimbursement form: taxi 2400 INR on 25 Oct"
python scripts/smoke.py cua                                         # check computer use against the real API
```

It never types passwords, one-time codes, card or ID numbers, stops at CAPTCHAs ("you do this part"), and always asks before Submit, with the approval bound to a hash of every value. Afterwards it checks the confirmation page and saves a screenshot. Set `LANTERN_FORMS_MODEL=muse-wonder-1.0` for final runs and `LANTERN_FORMS_HEADLESS=0` to watch it work.

## Your phone: WhatsApp (and Telegram)

Lantern's WhatsApp number is a **personal task and approval console for your own computer, not a general chatbot** (WhatsApp's January 2026 rule on AI providers). Only your number is listened to. Send it a task or a voice note (Ray-Ban Meta: "Hey Meta, send a voice message to Lantern on WhatsApp"). Approvals arrive as **Approve / Deny / Show** buttons, and **STOP** halts everything.

Setup (once):
1. developers.facebook.com → create an app → add the **WhatsApp** product → note the test number's **Phone number ID**; add your own number as a recipient.
2. Business settings → System users → create one, give it the app with `whatsapp_business_messaging`, and generate a **permanent token** (not the 24-hour one). App settings → Basic → **App secret**.
3. Put them in `.env`: `LANTERN_CHANNELS=whatsapp`, `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN` (any random string), `LANTERN_WHATSAPP_OWNER` (your number, digits only, with country code).
4. `python -m lantern --serve`, then `cloudflared tunnel --url http://127.0.0.1:8765`.
5. In the app's WhatsApp → Configuration: callback URL `https://<tunnel>/webhook/whatsapp`, the same verify token, and subscribe to **messages**.
6. Send "hi" to the test number (this opens WhatsApp's 24-hour window), then run `python -m lantern --channel-test "Lantern here"`.

Telegram fallback: create a bot with @BotFather, then set `LANTERN_CHANNELS=telegram`, `TELEGRAM_BOT_TOKEN` and `LANTERN_TELEGRAM_OWNER` (your chat id).

## Email

`send_email` asks first, with a card showing the recipients, subject, body preview and attachments (private files flagged). It warns if a recipient only appeared inside a file or web page. It sends over SMTP and checks the Sent folder over IMAP before calling it verified. Gmail setup: turn on 2-step verification, create an **app password**, then set `LANTERN_EMAIL_MODE=smtp`, `LANTERN_SMTP_USER` and `LANTERN_SMTP_PASSWORD` in `.env`.

## Desktop

`cd desktop && npm install && npm start` opens the avatar and panel. Run it next to `python -m lantern --serve`. `npm run mock` rehearses the whole story offline. Details: [desktop/README.md](desktop/README.md).

## Lantern's face (made with Muse)

```bash
python scripts/make_avatar.py                         # Muse Image: canonical portrait + 6 anchored expressions (~$0.07)
python scripts/make_avatar.py --canonical my_art.png  # start from a character you made in the MetaMuse app
python scripts/import_avatar.py ~/Downloads/muse/     # use MetaMuse / Vibes exports (idle.png, celebrating.mp4, ...)
```

## Your projects

```bash
python -m lantern --which torch      # which projects use torch? answered from the local map, no model call
python -m lantern --projects
```
Lantern can also run a project's tests or `pip install` one package, always inside that project's own virtual env and only after you approve.

## MCP (ready for Meta AI Connectors)

- **Lantern as a server:** `python -m lantern --mcp` speaks MCP on stdio. Another AI app gets Lantern's read-only tools (folders, search, read open files, ask about private files via the local model, projects) plus `submit_task`. Every change is still approved by you, on the desktop or WhatsApp, and everything it hands out is scrubbed and shows up in "What left my laptop today".
- **Lantern as a client:** list servers in `~/.lantern/mcp.json`. Their tools are offered to Muse Spark as **deferred tools** found through **tool search**, so a big catalog costs nothing until it's used. Each call asks you first, unless you mark the tool read-only:

```json
{"servers": {"notes": {"command": "python", "args": ["-m", "my_notes_mcp"], "read_only": ["search"]}}}
```

## Tests

```bash
python -m pytest -q     # 16 tests, offline, using a scripted fake model
```

## Roadmap

| Days | Piece |
|---|---|
| Oct 26 | ✅ Agent loop, memory, safe file tools |
| Oct 27–29 | Phone control (WhatsApp), email sending, voice notes via Muse Voice Transcribe |
| Oct 30 – Nov 1 | Desktop avatar, widget builder, Muse Image expressions |
| Nov 2 | Ray-Ban Meta glasses path, SAM 3.1 camera check-ins |
| Nov 3–4 | Polish, demo video, submit |

See `BUILD_LOG.md` for what was built when.
