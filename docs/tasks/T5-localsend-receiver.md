# T5: Receive from phones over home Wi-Fi (LocalSend protocol, our own implementation)

**Goal:** on the phone, **Share → LocalSend → "Lantern on <laptop>"** sends a file, a photo or a link straight to Lantern over the home Wi-Fi. No Telegram and no cloud are needed, and it works on iPhone and Android.
- **Files** land in an inbox folder after one approval.
- **A link** turns into the T1 "open on my laptop" approval.

LocalSend is an open-source app (MIT) with a **public protocol spec** at https://github.com/localsend/protocol (README).

## Rule for this card: build it ourselves
- Implement **only from the protocol README** (v2.x).
- **Do not read or copy code** from the LocalSend app or from any other implementation.
- Standard library (`http.server`, `ssl`, `socket`, `json`, `secrets`) plus `cryptography` (already a dependency) for the self-signed certificate.
- Record it in `docs/THIRD_PARTY.md` as "LocalSend protocol (spec only, MIT): implemented from the public README; no code used", and add a `DECISIONS.md` entry.

Read `CLAUDE.md`. `python -m pytest -q` must pass before committing.

## Build: `lantern/localsend.py` (receiver only, for now)
1. **Off by default.** It starts only when `LANTERN_LOCALSEND=1` or the panel switch is on (Setup → "Receive from phones on Wi-Fi"). When off, nothing listens.
2. **Identity:**
   - alias `Lantern on <hostname>`, deviceType `desktop`;
   - a self-signed TLS certificate made once and kept in `~/.lantern/localsend/`;
   - the fingerprint as the spec defines it (SHA-256 of the certificate).
3. **Discovery** as in the spec: listen on the multicast group and port the README gives, answer announcements, announce ourselves on start. Handle a missing network or a firewall-blocked socket gracefully, with a clear message in the doctor.
4. **HTTPS endpoints** from the spec: info, register, prepare-upload, upload, cancel (v2 paths).
   - **prepare-upload never auto-accepts.** It raises a Lantern **approval card** (desktop, and the phone channel if connected): "📱 Pixel 8 wants to send 2 files: photo.jpg (3.1 MB), notes.pdf (220 KB)". Accept or decline maps to the spec's accept/reject answers.
   - **Optional PIN** (spec feature): `LANTERN_LOCALSEND_PIN`.
   - Session ids and file tokens come from `secrets.token_urlsafe`, compared with `hmac.compare_digest`. Anything not issued by us is refused.
5. **Saving files:**
   - only through `lantern.policy` (`write_bytes`), into an inbox that must be a **writable zone** (default `~/Downloads/Lantern Inbox`; refuse to start if it isn't writable);
   - file names sanitized: no path separators, no `..`, no leading dots, length-limited, with a de-duplicated `name (2).ext`;
   - limits: 500 MB per transfer, 100 files, and upload size must equal the announced size or it is refused;
   - never overwrite.
6. **Text and links:** the spec sends text as a message with a preview. If it's a web link (`linkopen.clean_url`), submit "open this link on my laptop: <url>" so the T1 approval appears. Otherwise show the text in the panel. **All received content is untrusted data**, never instructions.
7. **The audit log** records what arrived: sender alias, names, sizes, and accepted/declined. No contents.
8. **Wiring:** start and stop with `--serve` (`lantern/cli.py`). The doctor gets a check, "LocalSend receiver: listening on port … / off", with a Windows Firewall hint.

## Tests (`tests/test_localsend.py`; in-process, no real network needed for most)
- info/register responses have the spec's fields;
- prepare-upload → an approval: declined gives the spec's reject and writes nothing; accepted issues tokens;
- an upload with a wrong token, an unknown session, or a size mismatch is refused;
- name sanitizing (`../../evil`, `.hidden`, very long names, duplicates);
- an inbox outside writable zones refuses to start;
- a link message submits the T1 goal;
- the PIN is required when set;
- off by default (nothing bound);
- the audit row has no file bytes.

## Manual check (laptop + phone on the same Wi-Fi)
1. Install LocalSend on the phone.
2. Turn the receiver on in Lantern.
3. Share a photo → "Lantern on <laptop>" → an approval card appears → Accept → the photo is in `Downloads\Lantern Inbox`.
4. Share a YouTube link → the T1 card appears → it opens.

**Commit:** "T5 LocalSend receiver: our own implementation of the public protocol, approvals, inbox via policy".
