"""What the paired phone may see: the same as WhatsApp, nothing more.

A task's private reply never reaches the phone: private-lane tasks show only
a placeholder (memory.PRIVATE_SUMMARY). Steps are folded summaries (tool names
and verified/error marks), never file contents or tool arguments. Approvals
are the WhatsApp card text (cards.approval_card) with the nonce the decision
needs, but never the raw payload.
"""
from __future__ import annotations

import time
from typing import Any

from .channels.cards import approval_card
from .memory import PRIVATE_SUMMARY


def _reply_public(core: Any, task_id: int, summary: str | None, steps: list[dict]) -> str:
    """The reply the phone may show: placeholder for private tasks, else the stored reply/summary."""
    try:
        if core.memory.is_private_task(task_id):
            return PRIVATE_SUMMARY
    except Exception:
        pass
    for step in reversed(steps):
        if step.get("kind") == "reply" and str(step.get("content", "")).strip():
            return str(step["content"])[:2000]
    return " ".join(str(summary or "").split())[:2000]


def _fold_steps(core: Any, task_id: int, steps: list[dict]) -> list[dict]:
    """Folded timeline for the phone: kinds and short labels, no arguments or file text."""
    folded: list[dict] = []
    private = False
    try:
        private = core.memory.is_private_task(task_id)
    except Exception:
        pass
    for s in steps:
        kind = str(s.get("kind", ""))
        if kind == "tool_call":
            folded.append({"kind": "step", "text": "working…"})
        elif kind == "tool_result":
            folded.append({"kind": "step", "text": "checked ✓"})
        elif kind == "reply":
            folded.append({"kind": "reply", "text": _reply_public(core, task_id, None, steps)[:500]})
        elif kind == "approval":
            folded.append({"kind": "step", "text": "asked for your OK"})
        elif kind == "note":
            text = str(s.get("content", ""))
            if text.startswith("interrupted:"):
                folded.append({"kind": "step", "text": text[:120]})
        elif kind == "plan":
            continue
        if len(folded) >= 20:
            break
    if private:
        return [f for f in folded if f.get("kind") != "reply"] + [{"kind": "reply", "text": PRIVATE_SUMMARY}]
    # De-duplicate the working markers so the phone shows a short folded list.
    out: list[dict] = []
    working = 0
    for f in folded:
        if f["kind"] == "step" and f["text"] in ("working…", "checked ✓"):
            working += 1
            if working > 6:
                continue
        out.append(f)
    return out


def task_view(core: Any, task_id: int) -> dict:
    """One task as the phone may see it. Never includes the private reply."""
    t = core.memory.get_task(task_id)  # KeyError if unknown
    steps = core.memory.steps(task_id)
    private = False
    try:
        private = core.memory.is_private_task(task_id)
    except Exception:
        pass
    reply = _reply_public(core, task_id, t.summary, steps)
    view: dict[str, Any] = {"id": t.id, "goal": str(t.goal).splitlines()[0][:200] if t.goal else "",
                            "status": t.status, "reply": reply,
                            "created_at": t.created_at, "updated_at": t.updated_at,
                            "private": bool(private),
                            "steps": _fold_steps(core, task_id, steps)}
    try:
        view["usage"] = core.memory.task_usage(t.id)
    except Exception:
        view["usage"] = {}
    return view


def tasks_view(core: Any, limit: int = 20) -> list[dict]:
    """Recent tasks as the phone may see them: no step contents at all."""
    out = []
    for t in core.memory.recent_tasks(min(max(limit, 1), 100)):
        private = False
        try:
            private = core.memory.is_private_task(t.id)
        except Exception:
            pass
        reply = PRIVATE_SUMMARY if private else " ".join(str(t.summary or "").split())[:300]
        out.append({"id": t.id, "goal": str(t.goal).splitlines()[0][:200] if t.goal else "",
                    "status": t.status, "reply": reply,
                    "created_at": t.created_at, "updated_at": t.updated_at, "private": bool(private)})
    return out


def approval_view(approval: Any, now: float | None = None) -> dict:
    """One pending approval as the phone may see it: the card text plus the
    nonce the decision needs. The raw payload is never included."""
    now = time.time() if now is None else now
    card = approval_card(approval, now)
    get = (lambda n, d=None: approval.get(n, d)) if isinstance(approval, dict) else (lambda n, d=None: getattr(approval, n, d))
    aid = str(get("id", "") or "")
    expires_at = float(get("expires_at", now) or now)
    return {"id": aid, "action": str(get("action", "") or ""),
            "title": card.text.splitlines()[0] if card.text else "Approve action",
            "text": card.text, "footer": card.footer,
            "expires_at": expires_at, "expires_in": max(1, int(round((expires_at - now) / 60))) ,
            "tainted": bool(get("tainted", False)), "flags": list(get("flags", []) or [])[:2],
            "nonce": str(get("nonce", "") or ""), "payload_hash": str(get("payload_hash", "") or "")}
