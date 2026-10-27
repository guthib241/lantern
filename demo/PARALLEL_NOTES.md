# Parallel session notes

A second session built these new folders alongside the core work. To avoid merge conflicts, it touched no existing file. Instead of `BUILD_LOG.md`, the log lives here.

- `demo/portal/`: S5 City Permit Portal
- `demo/attacks/`: prompt-injection test pack (S0.6)
- `desktop/board/`: S16 teaching board
- `tests/parallel/`: tests for all three

No Meta API calls; nothing here imports `lantern/`, so it stands alone on `main`.

## Log

### Oct 27: S5 City Permit Portal (`demo/portal/`)
- **Stack:** standard library only (`http.server`, `email` for multipart), on 127.0.0.1, all state in memory.
- **Flow:** start → personal → address → permit (dropdown with fees, date picker) → documents (ID proof dropdown, site-plan upload with type, size and magic-byte checks) → review (Change links, fee) → payment (counter or card) → confirmation with a reference `CPP-YYYY-XXXXXX`.
- **Payment hard stops:** card number, expiry, CVV, name on card, OTP and portal password are always on the payment page, but required only for card payment. "Pay at the counter" lets an agent finish without touching them. Only the card's last four digits are kept.
- **Layouts:** v1 has stable ids equal to the field keys. v2 (`?layout=v2`) changes the labels, ids, names and button texts, puts the buttons above the fields in swapped order, uses a dark serif style with a sidebar of steps, and has no autocomplete hints on the card fields (a hard-stop check must read labels). The server accepts both name sets, so switching mid-application loses nothing.
- **API for postconditions:** `/api/applications/<ref>` (includes the upload's SHA-256), `/api/reset`, `/api/trap-hits`.
- **Data:** `seed.json` holds the fake applicant (Asha Rao, matching the existing rehearsal); `fixtures/site_plan.pdf` is a 652-byte valid PDF.
