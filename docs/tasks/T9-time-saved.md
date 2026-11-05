# T9: "Time saved" counter

**Goal:** every finished task gets an honest estimate of the minutes it saved, and the Today tab shows **"Lantern saved you 2 h 15 min this week"**, with a small bar per day. People love a number that grows. **It must stay honest.**

Read `CLAUDE.md`. `python -m pytest -q` and `npm test` must pass.

## Build
1. **`lantern/timesaved.py`:** a **fixed, documented table** of minutes per verified action. For example:
   - move/organize a file: 0.5 min each, at most 20 per task;
   - fill a form: 6;
   - send an email: 3;
   - export a CAD part: 10;
   - open a link on the laptop: 0.5;
   - receive a file over LocalSend: 1.

   **Only actions with `verified: true` count.** Failed, declined or stopped tasks count 0. **Never ask a model for the estimate.**
2. **Storage:** the estimate is stored with the task (use the memory module's existing task fields or a small table; follow its migration pattern). Each receipt gets one line: "Estimated time saved: 6 min (form filled)".
3. **Route:** `GET /stats/time-saved?days=7` → total + per day.
4. **Panel → Today:** a new tile "Time saved this week" with the total and a 7-bar mini chart. Accessible text, plus the existing theme colours.
5. **`docs/TIME_SAVED.md`:** the table and the rule "estimates, not measurements".

## Tests
- verified actions count and unverified/declined don't;
- the per-task cap;
- the week total across days (fake clock);
- the receipt line;
- the route needs auth.

**Commit:** "T9 time saved: honest fixed-table estimates, weekly tile".
