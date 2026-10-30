# Building Lantern with Muse Code (Meta's coding agent)

Muse Code is Meta's coding agent for the terminal, built for Muse Spark (dev.meta.ai/docs/muse-code). It plans, edits and runs commands in this repo with approvals and a sandbox. It reads `CLAUDE.md` from the repo root automatically, so Lantern's hard rules apply to it too.

## Install (Windows, PowerShell, not Command Prompt)
```powershell
irm https://dev.meta.ai/install.ps1 | iex
```
What the installer does (read Oct 30, 2026):
- puts a `muse` command in `%LOCALAPPDATA%\Programs\muse`;
- adds that folder to your user PATH;
- downloads the program from Meta's servers (`api.meta.ai` / `lookaside.facebook.com`) and checks its SHA-256 checksum.

Open a new PowerShell window afterwards and run `muse --version`.

## Sign in and billing
- First run asks for a browser sign-in or an API key. **Use your API key.** It bills the same Model API credit as Lantern (pay as you go, per token).
- **Contributor mode:** Muse Code *subscriptions* default to Contributor mode, which lets Meta train on your prompts and code. If you subscribe, turn it **off** in Muse Code's settings (CLAUDE.md: never use Contributor mode on user data).
- **Don't run `muse init`.** It writes an `AGENTS.md`, which Muse Code reads *instead of* `CLAUDE.md`.

## Running a task card
```powershell
cd $HOME\lantern
.venv\Scripts\activate
git pull
muse
```
In the session, type:
```
Do the task in docs/tasks/T1-open-on-laptop.md exactly. Run python -m pytest -q before committing. Do not push.
```
- Approve its edits and commands as it asks; it shows each one first.
- When it says it's done, check for yourself: `python -m pytest -q`, `git log -1`, `git diff HEAD~1 --stat`.
- Then `git push`. Claude reviews the diff on GitHub before you build on it.

Headless (one shot, no chat): `muse exec "Do the task in docs/tasks/T1-open-on-laptop.md exactly. Run python -m pytest -q before committing. Do not push."`

## Task cards
| Card | Feature | Status |
|---|---|---|
| `docs/tasks/T1-open-on-laptop.md` | share a link from the phone → it opens on the laptop | to do |
| `docs/tasks/T2-send-to-phone.md` | "send this file to my phone" over Telegram | to do (needs T1's Telegram setup to try by hand) |
