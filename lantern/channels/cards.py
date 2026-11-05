"""Phone approval cards, written in code (never by a model).

One pure function, `approval_card(approval, now) -> Card`: the action in plain
words with a tier marker, the key facts as short "Label: value" lines taken
from the approval's existing fields, a line when the approval came from
untrusted content, and an expiry footer. `approval` is an Approval or its
public dict (the hub only has the dict); `now` is seconds since the epoch.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MAX_BODY = 1024    # WhatsApp interactive body
MAX_FOOTER = 60    # WhatsApp footer
MAX_TITLE = 20     # button titles

UNTRUSTED_LINE = "⚠️ came from a web page/file, not from you"
NEEDS_OK = "Lantern needs your OK"   # stays in every card (the footer), so the phone always says whose OK is needed

# Action -> plain words for the card's first line. Tiers come from
# security.TIERS (red: send/submit/run, yellow: reversible change).
PLAIN = {
    "send_email": "Send email",
    "submit_form": "Submit form",
    "fill_form": "Fill form",
    "write_file": "Create file",
    "move_file": "Move file",
    "organize_files": "Tidy files",
    "undo_moves": "Put files back",
    "run_in_project": "Run command",
    "set_reminder": "Set reminder",
    "cancel_reminder": "Cancel reminder",
    "remember": "Remember",
    "forget": "Forget",
    "make_widget": "Make widget",
    "save_widget": "Add widget",
    "run_routine": "Run routine",
    "reel_steps": "Do video steps",
    "cua_safety_check": "Check step",
    "open_untrusted_url": "Open web page",
    "network_send": "Send",
    "network_import": "Import routine",
    "lent_action": "Lent action",
    "mcp_call": "Use other program",
    "ask_user": "Answer needed",
}

# Payload keys that are hashes, sizes or bookkeeping: never a "fact" line.
SKIP_KEYS = {"sha256", "hash", "payload_hash", "args_sha256", "body_sha256", "hidden",
             "grant", "private", "overwrite", "chars", "bytes"}


@dataclass
class Card:
    """What goes to the phone: `text` (interactive body), `footer` (the expiry
    line; WhatsApp shows it as the message footer, other channels append it to
    the text), and Approve / Deny / Show `buttons` as [(id, title)]."""
    text: str
    footer: str
    buttons: list[tuple[str, str]]


def _get(approval: Any, name: str, default: Any = None) -> Any:
    if isinstance(approval, dict):
        return approval.get(name, default)
    return getattr(approval, name, default)


def _short(value: Any, n: int = 80) -> str:
    text = " ".join(str(value).split())
    return text[:n] + ("…" if len(text) > n else "")


def _short_path(path: Any, keep: int = 3) -> str:
    """A path as the owner thinks of it: the last few folders, never /tmp/... .
    '.../home/Downloads/Images' -> 'home/Downloads/Images'."""
    parts = [p for p in str(path).replace("\\", "/").split("/") if p not in ("", ".")]
    return "/".join(parts[-keep:]) if parts else str(path)


def _move_groups(moves: Any, limit: int = 4) -> list[tuple[str, int]]:
    """Destination folders with file counts, biggest first: [('home/Downloads/Images', 2)]."""
    groups: dict[str, int] = {}
    if isinstance(moves, list):
        for m in moves:
            if isinstance(m, dict) and m.get("to"):
                parent = str(m["to"]).replace("\\", "/").rsplit("/", 1)[0]
                key = _short_path(parent or m["to"])
                groups[key] = groups.get(key, 0) + 1
    return sorted(groups.items(), key=lambda g: -g[1])[:limit]


def _tier(approval: Any, action: str) -> str:
    tier = str(_get(approval, "tier", "") or "")
    if not tier:
        try:
            from ..security import tier_of
        except Exception:
            return ""
        tier = tier_of(action)
    return tier


def _facts(action: str, payload: Any) -> list[str]:
    """Key facts as 'Label: value' lines, from the payload's existing fields."""
    if not isinstance(payload, dict):
        return []
    lines: list[str] = []

    def add(label: str, value: Any, n: int = 80) -> None:
        text = _short(value, n)
        if text:
            lines.append(f"{label}: {text}")

    if action == "send_email":
        to = payload.get("to") or []
        add("To", ", ".join(to) if isinstance(to, list) else to)
        cc = payload.get("cc") or []
        if cc:
            add("Cc", ", ".join(cc) if isinstance(cc, list) else cc)
        add("Subject", payload.get("subject"))
        atts = payload.get("attachments") or []
        if atts:
            names = ", ".join(a.get("name", "?") for a in atts if isinstance(a, dict))
            add("Attachments", f"{len(atts)} ({names})" if names else str(len(atts)))
        return lines
    if action in ("submit_form", "cua_safety_check", "open_untrusted_url"):
        add("Site", payload.get("url"))
        if action == "submit_form":
            add("Button", payload.get("submit"))
            fields = payload.get("fields") or []
            if fields:
                names = ", ".join(f[0] if isinstance(f, (list, tuple)) else str(f) for f in fields[:6])
                add("Fields", f"{len(fields)} ({names})")
        return lines
    if action == "write_file":
        add("File", payload.get("path"))
        return lines
    if action == "move_file":
        add("Move", f"{payload.get('from')} → {payload.get('to')}")
        return lines
    if action in ("organize_files", "undo_moves"):
        moves = payload.get("moves") or payload.get("plan") or []
        total = len(moves) if isinstance(moves, list) else 0
        add("Files", str(total) if total else _short(moves, 60))
        for folder, n in _move_groups(moves):
            lines.append(f"→ {folder} ({n})")
        return lines
    if action == "run_in_project":
        add("Run", payload.get("display") or " ".join(payload.get("argv") or []) or payload.get("command"))
        add("In", payload.get("cwd") or payload.get("path"))
        return lines
    if action == "set_reminder":
        add("Reminder", payload.get("text"))
        due = payload.get("due_at")
        if due:
            try:
                from ..reminders import describe
                add("When", describe(float(due)))
            except Exception:
                pass
        repeat = payload.get("repeat")
        if repeat:
            add("Repeat", repeat)
        return lines
    if action == "cancel_reminder":
        add("Reminder", f"#{payload.get('id')}")
        return lines
    if action in ("remember", "forget"):
        add("Fact", payload.get("key"))
        if action == "remember":
            add("Value", payload.get("value"), 60)
        return lines
    if action in ("save_widget", "make_widget"):
        add("Widget", payload.get("name"))
        return lines
    if action == "run_routine":
        add("Routine", payload.get("routine"))
        red = payload.get("red") or []
        if isinstance(red, list) and red:
            add("Submits", str(len(red)))
        add("On", payload.get("target"))
        return lines
    if action == "reel_steps":
        add("Routine", payload.get("title"))
        steps = payload.get("steps") or []
        add("Steps", str(len(steps)) if isinstance(steps, list) else _short(steps, 60))
        return lines
    if action == "network_send":
        add("To", payload.get("to"))
        add("What", payload.get("routine") or payload.get("file"))
        return lines
    if action == "network_import":
        add("Routine", payload.get("routine"))
        add("From", payload.get("from"))
        return lines
    if action in ("lent_action", "mcp_call"):
        add("Tool", payload.get("tool"))
        return lines
    # Anything else: short scalar fields that aren't hashes or bookkeeping.
    for key, value in payload.items():
        if len(lines) >= 5:
            break
        if key in SKIP_KEYS or key.endswith(("_sha256", "_hash")):
            continue
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            add(key.replace("_", " ").capitalize(), value, 80)
    return lines


def approval_card(approval: Any, now: float) -> Card:
    """Format one approval card. Pure: no model, no I/O, no clock of its own."""
    action = str(_get(approval, "action", "") or "")
    aid = str(_get(approval, "id", "") or "?")
    nonce = str(_get(approval, "nonce", "") or "")
    payload_hash = str(_get(approval, "payload_hash", "") or "")
    payload = _get(approval, "payload", {})
    expires_at = float(_get(approval, "expires_at", now) or now)
    minutes = max(1, round((expires_at - now) / 60))

    tier = _tier(approval, action)
    marker = "🔴 " if tier == "red" else "🟡 " if tier == "yellow" else ""
    plain = PLAIN.get(action, action.replace("_", " ").capitalize() or "Approve action")
    lines = [f"{marker}{plain}"]

    for fact in _facts(action, payload)[:6]:
        lines.append(fact)
    if _get(approval, "tainted", False) or _get(approval, "flags", []):
        lines.append(UNTRUSTED_LINE)
    for flag in (_get(approval, "flags", []) or [])[:2]:
        text = _short(flag, 120)
        if text and text not in lines:
            lines.append(text)

    footer = f"{NEEDS_OK} · expires in {minutes} min · #{aid}"
    h8 = payload_hash[:8]
    buttons = [(f"approve:{aid}:{nonce}:{h8}", "✅ Approve"),
               (f"deny:{aid}:{nonce}:{h8}", "❌ Deny"),
               (f"show:{aid}", "🔎 Show")]
    text = "\n".join(lines)[:MAX_BODY]
    return Card(text=text, footer=footer[:MAX_FOOTER], buttons=buttons)
