"""Undo: put back the files a task moved, from its own verified log.

Every verified move (move_file, organize_files, and an undo itself) logs an "undo" step in memory with the
reverse move. Undoing a task replays those steps backwards (last move first, so chains unwind cleanly)
through Toolbox.undo_moves: the same zone checks, one approval card bound to every file's path, size and
modified time, and a check on disk after each move. No model is involved, so an undo costs nothing.
A file moved twice (a -> b -> c) goes straight home (c -> a); moved there and back, it's left alone.

Nothing here deletes anything, and a file is never put back over one that now sits in its old place.
"""
from __future__ import annotations

import json
import re

from .memory import Memory

UNDONE = "undone by task #"   # note on the original task once an undo moved files back
WORDS = re.compile(r"^\W*/?undo(\s+(that|it|last|the last task))?\W*$", re.I)   # typed or sent: undo the last task


class UndoError(ValueError):
    pass


def undo_plan(memory: Memory, task_id: int) -> list[dict]:
    """The moves that put task #task_id's files back, last move first. KeyError if there's no such task."""
    task = memory.get_task(task_id)
    if task.status in ("active", "queued"):
        raise UndoError("that task is still running")
    origin: dict[str, str] = {}   # where a file is now -> where it was before the task (chains collapse)
    for step in memory.steps(task_id):
        if step["kind"] == "note" and str(step["content"]).startswith(UNDONE):
            raise UndoError(f"task #{task_id} was already {step['content']}")
        if step["kind"] != "undo":
            continue
        try:
            moves = json.loads(step["content"]).get("moves") or []
        except (TypeError, ValueError, AttributeError):
            continue
        for m in moves:
            if isinstance(m, dict) and m.get("source") and m.get("destination"):
                now, before = str(m["source"]), str(m["destination"])
                origin[now] = origin.pop(before, before)
    plan = [{"source": now, "destination": before} for now, before in reversed(origin.items()) if now != before]
    if not plan:
        raise UndoError(f"task #{task_id} didn't move any files, so there's nothing to put back")
    return plan


def undoable(memory: Memory, task_id: int) -> bool:
    try:
        return bool(undo_plan(memory, task_id))
    except (UndoError, KeyError):
        return False
