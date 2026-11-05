# Time saved: estimates, not measurements

Every finished task gets an estimate of the minutes it saved you, and the
Today tab shows **"Lantern saved you 2 h 15 min this week"** with a small bar
per day. The number is meant to grow. It must stay honest, so the rules are
in code (`lantern/timesaved.py`), not in a prompt:

- **Fixed table.** The minutes per action live in one place and never change
  per task:

  | Verified action | Minutes |
  |---|---|
  | Move/organise a file | 0.5 each, at most 20 files per task |
  | Fill a form | 6 each |
  | Send an email | 3 each |
  | Export a CAD part | 10 each |
  | Open a link on the laptop | 0.5 each |
  | Receive a file over LocalSend | 1 each |

- **Verified only.** Only actions whose tool result says `verified: true`
  count. Failed, declined or stopped tasks count 0.
- **Never ask a model.** No model call is involved in the estimate, so nothing
  can talk it up. The per-task cap (20 files) stops one big tidy-up from
  dominating the week.
- **Stored with the task.** The estimate is written to the `time_saved` table
  when the task finishes (same migration pattern as the other tables: plain
  `CREATE TABLE IF NOT EXISTS`). Forgetting a task forgets its estimate too.
  LocalSend receives have no task, so verified uploads are counted from the
  audit log instead (`localsend.upload` rows are only written after the
  file passes its on-disk check).
- **Estimates, not measurements.** These are rough guesses at what each chore
  would have taken by hand. The week total reads as encouragement, not a
  timesheet — and the receipt says "Estimated".

Route: `GET /stats/time-saved?days=7` → `{days: [{day, minutes}], total_min,
total}` (needs the token, like every route but `/health`).
