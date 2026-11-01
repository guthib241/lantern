# Agent packs

An agent pack is a ready-made agent saved as **one small JSON file**
(`<name>.lantern-agent.json`, at most 32 KB). Instead of writing a prompt,
you fill in a few blanks ("Which folder?", "Outer size?") and Lantern does
the rest. You can share the file with someone else, and installing theirs
gives you the same agent.

Two packs ship with Lantern in `agents/`: `cad-enclosure` (a 3D-printable
box with mounting holes) and `tidy-downloads` (sorts a messy folder).

## What's inside

A pack is data only — never code. It holds a name, title, description,
author and version; up to 6 inputs (each with a question and an example);
a goal with `{blanks}` the user fills in; plain-English instructions;
and the list of tools the agent may use (`search_files`, `organize_files`,
`freecad.*` for the CAD tools, and so on). There is deliberately no way to
pick a model: a pack can never send your data somewhere unexpected or run
up a surprise bill.

## Safety rules (enforced in code, not promised in words)

- **Installing asks first.** The card shows the title, author, version,
  every tool the pack may use, and the full instructions. The yes is bound
  to the file's SHA-256: if a single byte changes, the approval no longer
  counts.
- **A pack can't do more than its list.** When it runs, Lantern refuses any
  tool outside the list — `freecad.*` covers the CAD tools, nothing else.
- **A shared pack is treated as untrusted.** It was written by someone else,
  so the run starts tainted: email addresses in it don't count as yours,
  and every approval still happens as usual. A pack never skips one.
- **Packs are checked on the way in.** Unknown fields, bad names, a `model`
  field, an unknown tool, a `{blank}` with no matching input, an oversized
  file, or anything that looks like a secret — all refused.
- **No deletes.** Disabling an agent flips a flag in its file; the file stays.

## Signed packs (who made it, and whether it changed)

A pack can carry an Ed25519 signature. The keys live only on each laptop
(`~/.lantern/keys/pack-signing.key`, owner-only, encrypted with Lantern's
at-rest cipher when one is available); the private half never travels.
Signing covers the pack's canonical bytes, so the check is one question:
does this file still match what its author sealed?

- **Badges.** Each card shows `✓ Signed by achan · key 3F2A-91C0`
  (the first 8 hex chars of the key's SHA-256), `Unsigned: anyone could
  have changed it`, or nothing at all — a file edited after signing is
  refused, not listed.
- **Trust on first use.** Installing a signed pack from a new key asks
  `New author key 3F2A-91C0 (achan). Trust it?`; approving records the key
  in `~/.lantern/agents/trusted_keys.json`. The same author name on a
  different key warns in red. Unsigned packs still install.
- **Your own packs.** `POST /agents/{name}/sign` seals a pack with your key
  after an approval. On the Agents tab, **Share** offers to sign first when
  the pack's author is you.
- **Checked every run, not only at install.** The seal is re-verified on
  every load, so a file edited on disk afterwards refuses to run.

## In the desktop panel

The **Agents** tab lists installed packs with their tools. **Run** asks one
question per input, then starts an ordinary task you watch on the Today tab
(**Stop** there stops it too). **Add…** installs a `.lantern-agent.json` file
(approved on the card first); **Share** saves the exact file to send someone.
On a finished task, **Make an agent** drafts a new pack from what it did —
you edit the blanks and install it like any other pack.

## Loop agents (packs on a schedule)

A pack can carry an optional `schedule`, a suggestion for running on its own:

```json
"schedule": {"every": "day", "at": "09:00", "inputs": {"folder": "Downloads"}}
```

`every` is `day`, `weekday` (Monday–Friday), `week` or `hours`. Day-style
schedules take `at` as `HH:MM` local time; `hours` schedules take `hours`
(a number, at least 1 — the shortest loop is every hour). `inputs` fills the
goal's `{blanks}` for every run, so it must cover each one. Schedules are
validated like the rest of the pack: unknown fields, bad times, short
intervals and secrets are all refused.

A schedule is only a suggestion: it starts **OFF** when installed. Turning it
on in the Agents tab asks first — the card shows how often the pack runs and
the per-run budget — and each due run is an ordinary task with its own
timeline, receipt and **Stop**. Two runs of the same pack never overlap. Every
change still needs its own approval card (unanswered cards count as declined,
never approved), and each run is capped at `LANTERN_TASK_BUDGET_USD`
($0.10 by default, less than an interactive task's $3).

After **3 failed or stopped runs in a row**, the loop pauses itself and tells
you on the phone and the desktop. The Agents tab shows "next run at …" and
"last run: ok / failed / stopped" with its receipt, plus **Pause** / **Resume**
and **Run now**. From the phone, `loops` lists the loops and `pause <name>`
turns one off.
