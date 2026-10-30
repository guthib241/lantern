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
