"""A task's receipt: what it changed and whether each change was checked, what you approved (bound to which
payload), which models it used, what left the laptop and where to, and what it cost.

Built only from Lantern's own records: the task's steps in memory (encrypted), the audit log of every call
that left, and usage rows. No model is asked, so a receipt can't be talked into saying something nicer.
Forgetting a task forgets its receipt too.
"""
from __future__ import annotations

import json
from collections import Counter
from urllib.parse import urlparse

from . import models
from .audit import AuditLog
from .memory import Memory

DONE = {   # plain words for the tools that change something
    "write_file": "Saved a file", "move_file": "Moved a file", "organize_files": "Tidied files",
    "undo_moves": "Put files back", "fill_form": "Submitted a form", "send_email": "Sent an email",
    "remember": "Remembered a fact", "forget": "Forgot a fact", "make_widget": "Added a widget",
    "run_in_project": "Ran a command", "set_reminder": "Set a reminder", "cancel_reminder": "Cancelled a reminder",
    "run_routine": "Ran a learned routine",
}
ASKED = {   # what an approval card asked, in plain words
    "submit_form": "Submit the form", "send_email": "Send the email", "write_file": "Save the file",
    "move_file": "Move the file", "organize_files": "Tidy the files", "undo_moves": "Put the files back",
    "remember": "Remember a fact", "forget": "Forget a fact", "save_widget": "Add the widget",
    "run_in_project": "Run the command", "set_reminder": "Set the reminder", "cancel_reminder": "Cancel the reminder",
    "cua_safety_check": "Continue past a browser safety check", "mcp_call": "Use another program's tool",
    "run_routine": "Run the learned routine",
}
BY = {"desktop": "on the desktop", "fake": "on the simulated phone", "whatsapp": "on WhatsApp",
      "telegram": "on Telegram", "timeout": "by the 10-minute timeout", "stopped": "by STOP"}
KINDS = {"responses": "model", "chat.completions": "model", "asr.transcribe": "voice", "smtp.send": "email"}


def _where(destination: str) -> str:
    """Where a call went, as a person reads it: a host name, or what a stand-in stood in for."""
    u = urlparse(destination)
    if u.scheme == "rehearsal":   # the rehearsal model stands in for Meta's API; nothing leaves
        return "Meta's API"
    if u.scheme == "fake":        # fake email / phone / private lane: nothing leaves either
        return {"smtp": "the email outbox"}.get(u.netloc, u.netloc or destination)
    if u.scheme in ("http", "https") and u.hostname:
        return u.hostname
    return destination


def _kind(endpoint: str) -> str:
    if endpoint in KINDS:
        return KINDS[endpoint]
    if endpoint.startswith("mcp."):
        return "MCP"
    return "message" if endpoint.endswith(".send") else endpoint


def _json(text: str) -> dict:
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def build(memory: Memory, audit: AuditLog, task_id: int) -> dict:
    """The receipt for one task. KeyError if there's no such task."""
    t = memory.get_task(task_id)
    actions, looked, approvals, attempts, routine = [], 0, {}, [], None
    for step in memory.steps(task_id):
        if step["kind"] == "tool_result":
            data = _json(step["content"])
            name, r = data.get("name"), data.get("result")
            if name in (None, "finish") or not isinstance(r, dict):
                continue
            if name in DONE or "verified" in r:
                actions.append({"tool": name, "what": DONE.get(name, name), "verified": r.get("verified") is True,
                                "note": str(r.get("error") or r.get("reason") or "")[:160], "at": step["created_at"]})
            else:
                looked += 1   # reads and lookups: counted, not itemised
        elif step["kind"] == "attempt":   # the recovery story (recovery.py): what failed and what came next
            attempts.append(_json(step["content"]))
        elif step["kind"] == "routine":   # a learned routine ran (compiler.py)
            r = _json(step["content"])
            routine = {k: r.get(k) for k in ("routine", "version", "ok", "steps", "seconds", "healed", "failed")}
        elif step["kind"] == "approval":
            a = _json(step["content"])
            if not a.get("id"):
                continue
            rec = approvals.setdefault(a["id"], {"action": a.get("action"), "summary": str(a.get("summary", ""))[:300],
                                                 "payload_hash": a.get("payload_hash", ""), "tainted": bool(a.get("tainted")),
                                                 "status": "pending", "by": None, "used": False})
            if a.get("event") == "used":
                rec["used"] = True
            else:
                rec["status"], rec["by"] = a.get("status", rec["status"]), a.get("by") or rec["by"]
    sent: dict[str, dict] = {}   # one row per (lane, destination), in the order calls were made
    for rec in approvals.values():   # plain words, so the desktop and the phone say the same thing
        rec["asked"] = ASKED.get(rec["action"], rec["action"])
        rec["where"] = BY.get(rec["by"] or "", f"on {rec['by']}" if rec["by"] else "")
    for e in audit.for_task(task_id):
        row = sent.setdefault(f"{e['lane']} {e['destination']}", {
            "lane": e["lane"], "to": _where(e["destination"]),
            "simulated": e["destination"].startswith(("rehearsal://", "fake://")),
            "kinds": [], "calls": 0, "bytes": 0, "redactions": Counter()})
        row["calls"] += 1
        row["bytes"] += e["bytes"]
        if (kind := _kind(e["endpoint"])) not in row["kinds"]:
            row["kinds"].append(kind)
        row["redactions"].update(e["redactions"] or {})
    rows = [{**row, "redactions": dict(row["redactions"])} for row in sent.values()]
    left = [r for r in rows if r["lane"] != "local" and not r["simulated"]]
    usage = [{**u, "badge": models.badge(u["model"])} for u in memory.task_usage_by_model(task_id)]
    return {
        "task": {"id": t.id, "goal": t.goal, "status": t.status, "summary": t.summary,
                 "started_at": t.created_at, "finished_at": t.updated_at},
        "actions": actions, "looked": looked,
        "checked": sum(a["verified"] for a in actions), "unchecked": sum(not a["verified"] for a in actions),
        "approvals": list(approvals.values()),
        "recovery": attempts,
        "routine": routine,
        "models": usage, "cost_usd": round(sum(u["cost_usd"] or 0 for u in usage), 6),
        "sent": rows,
        "left_bytes": sum(r["bytes"] for r in left),
        "stayed_local": sum(r["calls"] for r in rows if r["lane"] == "local"),
        "redactions": dict(sum((Counter(r["redactions"]) for r in rows), Counter())),
    }


def _size(n: int) -> str:
    return f"{n:,} bytes" if n < 10_000 else f"{n / 1024:,.0f} KB"


def as_text(r: dict) -> str:
    """The receipt in a few plain lines (the phone, the CLI)."""
    t = r["task"]
    lines = [f"🧾 Task #{t['id']} · {t['status']}: {t['goal'].splitlines()[0][:80]}"]
    for a in r["actions"]:
        mark = "✓ checked" if a["verified"] else ("✗ " + a["note"] if a["note"] else "✗ not checked")
        lines.append(f"• {a['what']}: {mark}")
    if r["looked"]:
        lines.append(f"• Looked things up {r['looked']} time{'s' if r['looked'] != 1 else ''}")
    if rt := r.get("routine"):
        healed = rt.get("healed") or {}
        lines.append(f"• Learned routine {rt['routine']} v{rt['version']}: {rt['steps']} steps in {rt['seconds']} s"
                     + (f", {len(healed)} healed" if healed else ""))
    for a in r.get("recovery", []):
        rung = f", rung {a['rung']}" if a.get("rung") is not None else ""
        lines.append(f"• ↻ Attempt {a.get('attempt')}: {a.get('tool') or 'model'} {str(a.get('class', '')).replace('_', ' ')}"
                     f" → {str(a.get('strategy', '')).replace('_', ' ')}{rung}")
    for a in r["approvals"]:
        who = "You" if a["by"] not in ("timeout", "stopped") else "It was"
        how = f"{a['status']} {a['where']}".strip() + (", used once" if a["used"] else "")
        lines.append(f"• {who} {how}: {a['asked']} (bound to {a['payload_hash'][:12]})")
    def where(rows: list[dict]) -> str:
        return "; ".join(f"{_size(s['bytes'])} to {s['to']} ({', '.join(s['kinds'])})" for s in rows)
    away = [s for s in r["sent"] if s["lane"] != "local" and not s["simulated"]]
    simulated = [s for s in r["sent"] if s["lane"] != "local" and s["simulated"]]
    if away or not simulated:
        lines.append("• Sent: " + (where(away) if away else "nothing left this laptop"))
    if simulated:
        lines.append(f"• Would have sent: {where(simulated)}. Simulated: nothing actually left")
    if r["redactions"]:
        lines.append("• Redacted before sending: " + ", ".join(f"{k.replace('_', ' ')} ×{v}" for k, v in r["redactions"].items()))
    if r["stayed_local"]:
        n = r["stayed_local"]
        lines.append(f"• Stayed on this computer: {n} private-lane question{'s' if n != 1 else ''}")
    lines.append(f"• Cost: ${r['cost_usd']:.4f}" + (" · " + ", ".join(f"{u['badge']} ×{u['calls']}" for u in r["models"])
                                                     if r["models"] else ""))
    return "\n".join(lines)
