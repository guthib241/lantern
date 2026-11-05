"""Suggested next steps under a finished task's phone reply, written by Muse Spark.

Given ONLY the owner's own goal text and Lantern's public reply, Spark proposes
at most 2 short follow-up requests the owner might send next. A tap starts a
new task with the full suggested text (kept server-side under a short id; never
only in the button id). A suggestion is just a new request: it goes through the
normal agent and approvals.

One structured Chat Completions call (reader.ask_json on the Spark-low rung,
through Lantern's audited client, cost logged to the task). Test chat doubles
route by schema name, so the call never touches the agent's scripted turns.
Skipped when disabled (`LANTERN_PHONE_SUGGESTIONS=0`), when the reply is
private, when there is no model client, or when the task's budget is spent.
"""
from __future__ import annotations

import json
import os
from typing import Any

RULES = """You suggest what the user might ask Lantern next. You get the user's own request and Lantern's public
reply, and nothing else. Answer with only the JSON object the schema asks for. suggestions: at most 2 follow-up
requests the user might plausibly send next, in order. Each: title (the button label, at most 20 characters) and
request (the full request the user would type, one short sentence, at most 300 characters; only words from the
request and reply, no new facts, files or people). Suggest nothing unrelated, and an empty list when nothing
follows naturally."""
SCHEMA = {
    "type": "object",
    "properties": {"suggestions": {"type": "array", "maxItems": 2, "items": {"type": "object", "properties": {
        "title": {"type": "string"}, "request": {"type": "string"}},
        "required": ["title", "request"], "additionalProperties": False}}},
    "required": ["suggestions"], "additionalProperties": False,
}
MAX_TITLE = 20


def enabled(env: dict | None = None) -> bool:
    """On by default; `LANTERN_PHONE_SUGGESTIONS=0` turns suggestions off."""
    v = (env if env is not None else os.environ).get("LANTERN_PHONE_SUGGESTIONS", "1")
    return v.strip().lower() not in ("0", "off", "no", "false")


def over_budget(core: Any, task_id: int) -> bool:
    """The task already spent its per-task budget: no model call for suggestions."""
    try:
        limits = core.budget.limits
        task_limit = limits.task
    except AttributeError:
        return False
    if task_limit is None:
        return False
    try:
        return core.memory.task_usage(task_id)["cost_usd"] >= task_limit
    except Exception:
        return False


def shape(data: Any) -> list[tuple[str, str]]:
    """Hold the answer to its shape: [(button title <= 20, full request)]."""
    out: list[tuple[str, str]] = []
    if not isinstance(data, dict):
        return out
    for raw in (data.get("suggestions") or [])[:2]:
        if not isinstance(raw, dict):
            continue
        title = " ".join(str(raw.get("title", "")).split())[:MAX_TITLE].strip()
        request = " ".join(str(raw.get("request", "")).split())[:300].strip()
        if title and request:
            out.append((title, request))
    return out


def generate(core: Any, task_id: int, goal: str, reply: str) -> list[tuple[str, str]] | None:
    """Ask Muse Spark for follow-ups, through the audited client (cost logged to
    the task like other calls). None when disabled, over budget, without a model
    client, or when Spark has nothing usable to suggest."""
    if not enabled() or over_budget(core, task_id):
        return None
    from ..ladder import RUNGS
    from ..reader import ask_json
    goal = " ".join(str(goal).split())[:500]
    reply = " ".join(str(reply).split())[:1000]
    user = json.dumps({"goal": goal, "reply": reply}, ensure_ascii=False)
    data, _cost, _error = ask_json(core.client, memory=core.memory, task_id=task_id, rung=RUNGS[3],
                                   system=RULES, user=user, schema=SCHEMA, name="phone_suggestions",
                                   kind="suggest", emit=core.emit)
    if data is None:   # no client (rehearsal), budget spent, offline, unreadable: just no buttons
        return None
    return shape(data) or None
