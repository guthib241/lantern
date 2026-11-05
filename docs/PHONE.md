# Lantern on the phone

A paired, chat-only web app (PWA). The owner opens Lantern on their phone's
browser, pairs it once, adds it to the home screen, and gets the same
chat-only experience as the desktop's Today view: requests on the right,
Lantern's answers on the left (steps folded), the message box pinned at the
bottom. Nothing else is pushed onto that screen.

## Same Wi-Fi (no HTTPS)

Lantern's server binds 127.0.0.1 by default and keeps doing so. To serve the
phone on the LAN:

```sh
lantern --serve --phone
# or
LANTERN_PHONE=1 lantern --serve
```

Rehearsal mode works with no key:

```sh
lantern --serve --demo --phone
```

Startup prints the LAN URL, e.g. `http://192.168.1.20:8765/m`. Open it on the
phone (same Wi-Fi). When `--phone` is on, every route except `/health`,
`/m`, `/m/*`, `POST /pair` and the webhooks refuses non-local clients with
403; desktop token routes (including `/pair/start` and `/devices`) stay
localhost-only whatever the token.

## HTTPS (home-screen install)

Most phones need HTTPS for "Add to Home Screen" with a service worker. Run
your own tunnel on the laptop, for example:

```sh
cloudflared tunnel --url http://127.0.0.1:8765
```

or Tailscale. Open `https://<your tunnel>/m` on the phone. The tunnel is
yours; Lantern never phones home.

## Pairing

1. On the desktop: Settings → "Pair a phone" → a 6-digit code (5 minutes,
   single use) and the `/m` URL.
2. On the phone: open `/m`, type the 6 digits. The phone stores a device
   token in localStorage.
3. A new "Pair a phone" replaces the old code. After 5 wrong tries the
   current code is burned; start a new one.

Only SHA-256 hashes of device tokens are stored, in
`~/.lantern/devices.json` (mode 0600; in rehearsal, next to the rehearsal
database).

## Revoking

Settings lists paired devices with Revoke, or:

```sh
curl -H "X-Lantern-Token: $(cat ~/.lantern/token)" localhost:8765/devices
curl -X POST -H "X-Lantern-Token: $(cat ~/.lantern/token)" localhost:8765/devices/<id>/revoke
```

A revoked phone gets 401 on every `/m/api` call and must pair again.

## Photos and voice notes

The composer has the same two extra inputs as WhatsApp:

- **📷 Photo.** Tap the camera button and take (or pick) a photo. Any words in
  the message box go with it as its caption, exactly like a WhatsApp caption:
  "check my work" sends it to Check my work, anything else (or nothing) sends
  it to the scan pipeline, which reads the paper and runs the learned routine
  it's for. The phone shrinks the photo first — at most 2000 px on the long
  side, as JPEG — so uploads stay small. The server checks the image type
  from the bytes themselves (JPEG, PNG or WebP), never from the file name,
  and refuses anything over 20 MB. `POST /m/api/photo` (device token; the raw
  image is the body, the caption is `?text=`; a multipart form with an
  `image` file part works too).
- **🎙 Voice note.** Hold the mic button, speak, let go to send (it stops by
  itself at 60 seconds). `POST /m/api/voice` (device token; the recording is
  the body) converts it on the laptop exactly like a WhatsApp voice note —
  ffmpeg to 16 kHz WAV, then Muse Voice Transcribe — shows the transcript
  back in the chat as your message ("🎙 …"), and handles it like typed text.
  Without ffmpeg or without a model key the reply says so plainly instead of
  failing quietly; in rehearsal (`--demo`) a scripted transcript keeps the
  flow testable with neither.

When a check-my-work task circles a line, the circled photo appears under
Lantern's answer in the chat. The phone fetches it from
`GET /m/api/tasks/{id}/image` with its device token: that route only serves
an image Lantern itself made in its media folder and recorded as that task's
check-work result, and it refuses private tasks outright.

## What the phone can and cannot see

The phone gets what WhatsApp gets (`lantern/phone.py`, one filter function):

- Tasks: goal, status, and the public reply. A private-lane task shows only
  "(answered from a private file; the details stay on this computer)".
  Steps are folded labels ("working…", "checked ✓"), never file contents or
  tool arguments. No receipts.
- Approvals: the WhatsApp card text (title, key facts, the untrusted line,
  expiry) with the nonce the decision needs. The raw payload (form values,
  recipients beyond the card) is never sent. Allow sends the nonce exactly
  like the desktop, through the same `ApprovalBus.decide` nonce/payload-hash
  check; a wrong nonce is refused.
- Reminders (list/cancel), Stop everything, live events over `/m/events`.
- It cannot: change settings, zones, memory, profile, widgets, audit log,
  costs, or reach any desktop-token route.
