"""Honest time-saved estimates: a fixed, documented table of minutes per verified action.

Every finished task gets an estimate of the minutes it saved, and the Today tab
shows "Lantern saved you 2 h 15 min this week". The numbers are fixed here, in
code — never asked of a model, so nothing can talk them up — and only actions
with ``verified: true`` count. Failed, declined or stopped tasks count 0.

Documented table (also in ``docs/TIME_SAVED.md``):
  move/organise a file ......... 0.5 min each, at most 20 files per task
  fill a form .................. 6 min each
  send an email ................ 3 min each
  export a CAD part ............ 10 min each
  open a link on the laptop .... 0.5 min each
  receive a file over LocalSend  1 min each

These are estimates, not measurements: rough guesses at what the chore would
have taken by hand, so the week total reads as encouragement, not a timesheet.
"""
from __future__ import annotations

import time

# Minutes per verified action. Fixed: change them here, with a reason, not per task.
MOVE_MIN = 0.5        # one moved/renamed file (tool: move_file)
ORGANIZE_MIN = 0.5    # one file tidied (tool: organize_files, per result["moved"])
MOVE_CAP = 20         # at most this many moved/organised files count per task
FORM_MIN = 6.0        # a submitted + verified form (tool: fill_form)
EMAIL_MIN = 3.0       # an email found in Sent afterwards (tool: send_email)
EXPORT_MIN = 10.0     # a CAD part exported and verified (tool: freecad.export)
LINK_MIN = 0.5        # a link opened on the laptop (tool: open_link)
RECEIVE_MIN = 1.0     # a file received over LocalSend (audit: localsend.upload)

TABLE = (
    ("move/organise a file", MOVE_MIN, f"each, at most {MOVE_CAP} per task"),
    ("fill a form", FORM_MIN, "each"),
    ("send an email", EMAIL_MIN, "each"),
    ("export a CAD part", EXPORT_MIN, "each"),
    ("open a link on the laptop", LINK_MIN, "each"),
    ("receive a file over LocalSend", RECEIVE_MIN, "each"),
)

FILE_TOOLS = {"move_file", "organize_files"}


def _files_moved(tool: str, result: dict) -> int:
    """How many files one verified file action counts for (capped later, per task)."""
    if tool == "move_file":
        return 1
    return result["moved"] if isinstance(result.get("moved"), int) else 0


def _label(tool: str, n: int) -> str:
    if tool == "move_file":
        return "file moved" if n == 1 else f"{n} files moved"
    if tool == "organize_files":
        return "file organised" if n == 1 else f"{n} files organised"
    if tool == "fill_form":
        return "form filled"
    if tool == "send_email":
        return "email sent"
    if tool == "freecad.export":
        return "CAD part exported"
    if tool == "open_link":
        return "link opened"
    return tool


def estimate_actions(actions: list[tuple[str, dict]]) -> tuple[float, list[str]]:
    """Minutes saved by verified tool actions. ``actions`` is (tool name, result dict).

    Only ``result.get("verified") is True`` counts; anything else (failed,
    declined, stopped, unchecked) adds nothing. Moved/organised files share one
    per-task cap. Returns (minutes, short labels for the receipt line).
    """
    files, minutes, labels = 0, 0.0, []
    file_kinds: set[str] = set()
    for tool, result in actions:
        if not isinstance(result, dict) or result.get("verified") is not True:
            continue
        if tool in FILE_TOOLS:
            files += max(0, _files_moved(tool, result))
            file_kinds.add(tool)
        elif tool == "fill_form":
            minutes += FORM_MIN
            labels.append(_label(tool, 1))
        elif tool == "send_email":
            minutes += EMAIL_MIN
            labels.append(_label(tool, 1))
        elif tool == "freecad.export":
            minutes += EXPORT_MIN
            labels.append(_label(tool, 1))
        elif tool == "open_link":
            minutes += LINK_MIN
            labels.append(_label(tool, 1))
        # Anything else changed nothing the table prices (or isn't priced): ignore.
    files = min(files, MOVE_CAP)
    if files:
        minutes += files * MOVE_MIN
        kind = "move_file" if file_kinds == {"move_file"} else "organize_files"
        labels.insert(0, _label(kind, files))
    return minutes, labels


def estimate_steps(steps: list[dict]) -> tuple[float, list[str]]:
    """Minutes saved by a task's timeline: verified ``tool_result`` steps only."""
    import json

    actions = []
    for step in steps:
        if step.get("kind") != "tool_result":
            continue
        try:
            data = json.loads(step.get("content", ""))
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        name, result = data.get("name"), data.get("result")
        if name in (None, "finish") or not isinstance(result, dict):
            continue
        actions.append((name, result))
    return estimate_actions(actions)


def format_minutes(minutes: float) -> str:
    """Short human form: "0 min", "30 sec", "6 min", "1 h", "2 h 15 min"."""
    total = max(0.0, minutes)
    if total <= 0:
        return "0 min"
    if total < 1:
        return f"{total * 60:g} sec"
    if total < 60:
        return f"{total:g} min"
    hours, rest = divmod(total, 60)
    return f"{hours:g} h" if rest < 1 else f"{hours:g} h {rest:g} min"


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def summarize(memory, audit, days: int = 7, now: float | None = None) -> dict:
    """Week total for ``GET /stats/time-saved``: per finished task plus per receive.

    Task estimates come from the stored ``time_saved`` table (written when each
    task finishes, so the route never re-reads timelines); LocalSend receives
    have no task, so verified uploads are counted from the audit log
    (``localsend.upload`` rows are only written after the on-disk check passes).
    """
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 31:
        raise ValueError("days must be a whole number from 1 to 31")
    now = time.time() if now is None else now
    since_day = _day(now - (days - 1) * 86400)
    per_day: dict[str, float] = {}
    for entry in memory.time_saved_since(0):
        day = _day(entry["updated_at"])
        if day >= since_day and entry["minutes"] > 0:
            per_day[day] = per_day.get(day, 0.0) + entry["minutes"]
    for row in audit.entries(since=now - days * 86400, limit=100_000):
        if row.get("endpoint") == "localsend.upload" and _day(row.get("ts", 0)) >= since_day:
            day = _day(row["ts"])
            per_day[day] = per_day.get(day, 0.0) + RECEIVE_MIN
    ordered = []
    for back in range(days - 1, -1, -1):
        day = _day(now - back * 86400)
        ordered.append({"day": day, "minutes": round(per_day.get(day, 0.0), 2)})
    total = round(sum(d["minutes"] for d in ordered), 2)
    return {"days": ordered, "total_min": total, "total": format_minutes(total)}
