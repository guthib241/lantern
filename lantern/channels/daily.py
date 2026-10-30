"""The short daily summary for the phone, written by Muse Spark.

Facts come from Lantern's own records (memory: today's tasks with status, the
reminders due, today's spend, today's denied approvals) gathered in code into a
small dict; Spark then writes at most 5 short lines in Lantern's persona from
ONLY those facts (no file contents, no private replies). When the model call
fails or there is no client, a plain code-written summary goes out instead.

Sent on demand ("summary", or the menu row) and once a day at
`LANTERN_DAILY_SUMMARY` (default "21:00" local; "off" disables).
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

DEFAULT_AT = "21:00"
ENV_NAME = "LANTERN_DAILY_SUMMARY"
MAX_TASKS = 30


def summary_at(env: dict | None = None) -> tuple[int, int] | None:
    """The daily time (local), or None when it's off."""
    v = (env if env is not None else os.environ).get(ENV_NAME, DEFAULT_AT)
    if v is None:
        return None
    v = str(v).strip().lower()
    if v in ("", "off", "no", "0", "none"):
        return None
    try:
        h, m = (int(x) for x in v.split(":"))
    except ValueError:
        return None
    return (h, m) if 0 <= h < 24 and 0 <= m < 60 else None


def due(sent: set[str], now: float | None = None, env: dict | None = None) -> bool:
    """Past today's time, and no summary sent yet today (local days)."""
    at = summary_at(env)
    if at is None:
        return False
    now = time.time() if now is None else now
    lt = time.localtime(now)
    start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, at[0], at[1], 0, 0, 0, -1))
    if now < start:
        return False
    return time.strftime("%Y-%m-%d", lt) not in sent


def _day_start(now: float) -> float:
    lt = time.localtime(now)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def _verified(memory: Any, task_id: int) -> bool:
    """Any tool result in this task said verified: true."""
    try:
        steps = memory.steps(task_id)
    except Exception:
        return False
    for step in steps:
        if step.get("kind") != "tool_result":
            continue
        try:
            result = json.loads(step["content"]).get("result") or {}
        except (TypeError, ValueError):
            continue
        if isinstance(result, dict) and result.get("verified") is True:
            return True
    return False


def _denied(memory: Any, since: float) -> list[dict]:
    """Approvals the owner denied since `since` (from task timelines)."""
    out: list[dict] = []
    try:
        tasks = memory.recent_tasks(MAX_TASKS)
    except Exception:
        return out
    for task in tasks:
        try:
            steps = memory.steps(task.id)
        except Exception:
            continue
        for step in steps:
            if step.get("kind") != "approval" or step.get("created_at", 0) < since:
                continue
            try:
                content = json.loads(step["content"])
            except (TypeError, ValueError):
                continue
            if content.get("event") == "decided" and content.get("status") == "denied":
                out.append({"action": str(content.get("action", "")),
                            "summary": " ".join(str(content.get("summary", "")).split())[:100]})
                if len(out) >= 5:
                    return out
    return out


def gather_facts(core: Any, now: float | None = None) -> dict:
    """Today's tasks (status/verified, no private replies), reminders due
    tomorrow, today's spend, denied approvals. Only Lantern's own records."""
    now = time.time() if now is None else now
    memory = core.memory
    start = _day_start(now)
    tomorrow_end = start + 2 * 86400
    tasks: list[dict] = []
    try:
        recent = memory.recent_tasks(100)
    except Exception:
        recent = []
    for task in recent:
        if task.created_at < start:
            continue
        goal = " ".join(str(task.goal).splitlines()[0].split())[:80]
        item: dict[str, Any] = {"id": task.id, "goal": goal, "status": task.status,
                                "verified": _verified(memory, task.id)}
        if not memory.is_private_task(task.id):
            item["summary"] = " ".join(str(task.summary or "").split())[:160]
        tasks.append(item)
        if len(tasks) >= MAX_TASKS:
            break
    try:
        reminders = [{"text": " ".join(r.text.split())[:80], "due": r.public()["due"]}
                     for r in memory.reminders() if r.due_at <= tomorrow_end][:5]
    except Exception:
        reminders = []
    try:
        spend = memory.spent(time.strftime("%Y-%m-%d", time.localtime(now)))
    except Exception:
        spend = 0.0
    return {"tasks": tasks, "reminders": reminders,
            "spend_today_usd": round(float(spend), 4), "denied": _denied(memory, start)}


RULES = """You write Lantern's daily summary for the user's phone. You get ONLY a JSON object of facts Lantern
recorded itself (today's tasks with status, reminders, today's model spend, denied approvals). Write at most 5
short lines in the persona you are given: what got done, what still needs the user, what's next. Only use the
facts given; never mention file contents, and task summaries marked missing are private: never guess them.
Answer with only the JSON object the schema asks for."""
SCHEMA = {
    "type": "object",
    "properties": {"lines": {"type": "array", "maxItems": 5, "items": {"type": "string"}}},
    "required": ["lines"], "additionalProperties": False,
}


def fallback(facts: dict) -> str:
    """A plain code-written summary: the same facts, no model."""
    tasks = facts.get("tasks") or []
    done = sum(1 for t in tasks if t.get("status") == "done")
    lines = [f"☀️ Today: {len(tasks)} task{'s' if len(tasks) != 1 else ''}, {done} done."]
    for task in tasks[:3]:
        mark = "✓" if task.get("status") == "done" else task.get("status", "?")
        lines.append(f"• #{task.get('id')} {mark}: {task.get('goal', '')[:60]}")
    reminders = facts.get("reminders") or []
    if reminders:
        lines.append("⏰ Next: " + "; ".join(f"{r['due']} {r['text'][:40]}" for r in reminders[:2]))
    denied = facts.get("denied") or []
    if denied:
        lines.append(f"🚫 Denied {len(denied)} approval{'s' if len(denied) != 1 else ''} today.")
    lines.append(f"💰 Today's model spend: ${float(facts.get('spend_today_usd') or 0.0):.4f}".rstrip("0").rstrip("."))
    return "\n".join(lines[:6])


def write(core: Any, facts: dict) -> str | None:
    """Ask Muse Spark for the summary (one structured chat call, cost logged
    like other calls). None when the model call fails or there is no client:
    the caller sends the fallback instead."""
    from ..agent import PERSONAS
    from ..ladder import RUNGS
    from ..reader import ask_json
    persona = PERSONAS.get(getattr(getattr(core, "cfg", None), "persona", ""), "")
    user = json.dumps({"persona": persona, "facts": facts}, ensure_ascii=False)
    data, _cost, _error = ask_json(core.client, memory=core.memory, task_id=None, rung=RUNGS[3],
                                   system=RULES, user=user, schema=SCHEMA, name="daily_summary",
                                   kind="daily_summary", emit=core.emit)
    if not isinstance(data, dict):
        return None
    lines = [" ".join(str(line).split())[:200] for line in data.get("lines") or []]
    lines = [line for line in lines if line][:5]
    return "\n".join(lines) if lines else None


def render(core: Any, now: float | None = None) -> str:
    """The daily summary: Spark's lines from Lantern's own facts, or the fallback."""
    facts = gather_facts(core, now)
    return write(core, facts) or fallback(facts)
