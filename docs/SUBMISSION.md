# Submission kit

Deadline: **Nov 4, 9:00 PM ET** (Nov 5, 7:30 AM IST). Plan to submit Nov 4 evening IST.

Slide for the video: [`docs/slide/architecture.png`](slide/architecture.png) (1920×1080; source `slide/architecture.html`).

## 1. Before you record (laptop)

```bash
lantern --doctor                      # every line ✓ for "Real demo" (or you know why not)
python scripts/smoke.py               # one real call per model: all OK
python scripts/smoke.py cua           # paste the LANTERN_CUA_… line it prints into .env
cd desktop && npm run rehearsal       # rehearse the whole story with the desktop app, $0
```

Then set these for recording (in `.env`): `LANTERN_FORMS_MODEL=muse-wonder-1.0`, `LANTERN_FORMS_HEADLESS=0` (the browser is visible), `LANTERN_CHANNELS=whatsapp` (plus the WhatsApp variables), and the private lane: `LANTERN_GLIMMER_URL` for Glimmer on this laptop, or for the self-hosted private Glimmer server `LANTERN_GLIMMER_URL` + `LANTERN_GLIMMER_KEY_FILE` ([MODAL.md](MODAL.md); ask it one private question a minute before recording, so the 22 s cold start is out of the way). Start the tunnel (`cloudflared tunnel --url http://127.0.0.1:8765`) and `cd desktop && npm start`, which starts `lantern --serve` too. Starting the server in its own terminal also works, and keeps its log in view.

On the screen:
- 1920×1080, light mode, Do Not Disturb on, an empty desktop, and the browser window on the left half.
- Planted for the story:
  - `~/Documents/taxi_receipt.txt`
  - a notes file with a fake key, e.g. copy `demo/home/Documents/project_notes.md`
  - `~/Private/offer_letter.txt` in a **private** zone (`lantern --set-zone ~/Private private`)
  - a project with torch installed
- Remember the organiser's email once: `lantern "remember organiser email is organiser@openbuild.example"`.
- Do one full real run before recording, and note the cost (`lantern --usage`).

## 2. The 3-minute video, shot by shot

| Time | On screen | You say | What Lantern shows |
|---|---|---|---|
| 0:00–0:12 | Desktop. Lantern's orb and the menu bar lantern | "I hate forms. So my AI lives on my laptop, does the boring work, and asks before anything changes." | Idle orb; menu bar: *Idle* |
| 0:12–0:30 | Walking, glasses (phone recording) | "Hey Meta, send a voice message to Lantern on WhatsApp: fill the hackathon reimbursement form for my taxi and email it to the organiser." | WhatsApp: 🎙️ the transcript, then "On it" |
| 0:30–1:05 | Laptop: the browser fills the form live; the island above the orb | (quiet, let it work) "It reads my profile, finds the receipt, and uses Muse Wonder to handle the fiddly dropdown. It never types passwords, codes, card or ID numbers." | Island: *Looking at the screen · Wonder*, *Clicking at…*; panel steps with model badges |
| 1:05–1:25 | Phone: **Approve / Deny** card; the panel's approval sheet with the field table | "Nothing gets submitted until I say so, and the approval is bound to exactly these values." | Island: *Submit this form?* with **Review…** only (it read a web page) |
| 1:25–1:40 | Tap **Approve** on the phone | "Approved." | Island: *Done · verified*; the email step shows ✓ (found in Sent) |
| 1:40–2:05 | ⌘⇧Space → Quick Ask: "What base salary does my offer letter say?" | Self-hosted: "My private folder never goes to a cloud AI API. Only Muse Glimmer reads it, on my own private server." (Glimmer on the laptop: "My private folder never leaves this laptop.") | Badge **Muse Glimmer (self-hosted)** (or **(local)** on the laptop) |
| 2:05–2:20 | Privacy tab | "Here's everything that left today: zero bytes of the letter went to Meta's API, and the API key in my notes was redacted before the call." | *0 bytes* of the private file on the cloud lane (the self-hosted lane is listed on its own); *Redacted: aws access key ×1* |
| 2:20–2:40 | Quick Ask: "Which projects use torch?", then "Make me a water timer" | "No model call for that one. And it writes little tools for me." | Answer from the local map; island **Allow** for the widget; the timer appears in Widgets |
| 2:40–3:00 | The slide (architecture + Meta model map) | "Muse Spark plans, Muse Wonder sees, Glimmer keeps private files off the cloud APIs, Voice Transcribe hears me, Muse Image drew its face. And it's an MCP server, ready for Meta AI Connectors." | `docs/slide/architecture.png` |

Keep the persona on **Smug** for the replies; it's part of the character.

**Optional swap for 2:20–2:40 (the Phase 2 centrepiece, measured live Oct 30):** run the permit portal task once (Lantern offers to learn it), run it again (*0 model calls, $0.00*), flip the portal to its v2 layout and run it again (*21 steps healed*, $0.025), then once more (*0 calls, $0.00*). Say: "The first time it thinks. After that it remembers, for free, and when the site changes it fixes itself." Pre-record the first run; show the $0 reruns and the heal live. Numbers: `scripts/phase2_live.py --only 6`.

**If something fails live:** record that beat again; don't fake it. Rehearsal mode (`--serve --demo`) is for practice. Its model is scripted, so don't present rehearsal footage as live model output. If a beat can't be made to work in time, cut it (cut order: SAM, then the widget, then the reminders).

## 3. Write-up (draft)

**Lantern: a private AI companion that lives on your laptop**

*Inspiration.* I hate forms, and I don't want an AI that sends my private folder to the cloud. I wanted an assistant that does real computer work, asks before anything changes, and can prove what left my laptop.

*What it does.* You ask by voice (WhatsApp, even from Ray-Ban Meta glasses), from Quick Ask (⌘⇧Space) or in the panel:
- Lantern fills web forms in its own browser, sends emails, tidies folders, sets reminders, answers "which projects use torch?", and writes small sandboxed widgets.
- Every change waits for your OK on the desktop or your phone, and the approval is bound to a hash of exactly what you saw.
- Then Lantern checks the result on disk or in the app (form confirmation page, Sent folder) and says whether it's verified.
- Changed your mind? **Undo** puts back every file a task moved: one tap, one approval, no model call.
- Every task has a **receipt**: what changed and whether it was checked, what you approved (with the payload hash), every byte that left and where, all from Lantern's own logs.

*How we built it, on Meta's models:*
- **Muse Spark 1.3** runs the agent (Responses API, tools, `previous_response_id`, tool search for MCP tools).
- **Muse Wonder 1.0** does computer use for the fiddly parts of forms.
- **Muse Glimmer** reads private-zone files on the user's own computer or a self-hosted private Glimmer server (Modal, A10: 22 s cold start, 29.6 tokens/s); the cloud APIs only ever see a placeholder for its answer, filled in on the user's screen.
- **Muse Voice Transcribe** handles voice notes.
- **Muse Image** made Lantern's face.
- **SAM 3.1** powers an objects-only camera check-in.
- The desktop app is Electron, with a live-activity island, Spotlight-style Quick Ask and menu bar integration. Lantern is also an MCP server.

*Safety, in code (not prompts):*
- Privacy zones (hidden / private / open / writable).
- A scrubber on every cloud payload, and an audit log of every byte sent.
- Single-use approvals with a 10-minute expiry, bound to the payload hash.
- Hard stops for password / OTP / card / ID fields and CAPTCHAs.
- Untrusted content (web pages, files, emails, MCP) is wrapped and can't give orders.
- No deletes, and STOP from anywhere.
- Spending limits checked before every model call ($150 total, $3 per task by default), so a runaway loop can't eat the credit.
- Memory is encrypted at rest, and forgetting really deletes.

*Challenges:*
- Getting computer use reliable on custom widgets.
- Making a Windows laptop behave like the Linux sandbox it was built in. Text files default to cp1252 there, and PowerShell saves `.env` as UTF-16. A test now refuses any file access without an explicit encoding.
- Making approvals readable *and* binding.
- Finding the bugs only an end-to-end run exposes. Examples: a secret at the start of a line slipping past a regex inside JSON; an email Message-ID that looked like a card number.

*What we learned.* Verification beats confidence: every action reports `verified: true/false`.

*What's next:*
- Meta AI Connectors via MCP.
- More form sites.
- Muse's own voice for spoken replies when an API exists.

## 4. Final checks

- [ ] `python -m pytest -q` and `cd desktop && npm test` green; `cd desktop && npm run e2e` (or `xvfb-run -a npm run e2e`) passes.
- [ ] No secrets in git: `git log -p | grep -nE "MODEL_API_KEY=.|LLM\\||WHATSAPP_TOKEN=.|SMTP_PASSWORD=."` prints nothing; `lantern --doctor` shows ".env stays out of git ✓".
- [ ] Repository access as the hackathon rules require. The repo is private, so if the rules need a public repo or judge access, set that up at submission time.
- [ ] Video ≤ 3:00, uploaded; slide included; the write-up pasted.
- [ ] README's "Cost so far" updated with the real number from `lantern --usage`.
