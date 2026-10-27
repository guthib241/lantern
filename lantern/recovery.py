"""Resilience engine (S2): what Lantern does when a step fails. Filled in by S2; S1 needs the idempotency rules.

A tool is idempotent here when running it a second time can't do anything twice: reading, or setting a value
to the same thing. Sends, submits and moves are not: they are never repeated unless a check proves the first
one didn't happen. Unknown tools (MCP tools from other programs) count as not idempotent.
"""
from __future__ import annotations

IDEMPOTENT = frozenset({
    "list_folders", "list_dir", "search_files", "read_file", "ask_private", "find_project", "project_info",
    "which_projects_use", "list_reminders", "remember", "forget", "cancel_reminder", "write_file", "finish",
})
NON_IDEMPOTENT = frozenset({"send_email", "fill_form", "move_file", "organize_files", "run_in_project",
                            "set_reminder", "make_widget", "undo_moves"})


def is_idempotent(name: str) -> bool:
    return name in IDEMPOTENT


def uncertain_result(name: str) -> dict:
    """What the model gets for a non-idempotent call that was running when Lantern stopped."""
    return {"error": f"Lantern stopped while {name} was running, so it may or may not have happened. "
                     "Check first (list the folder, look in the Sent mail, open the confirmation) and only "
                     "then decide whether to do it again.",
            "uncertain": True}
