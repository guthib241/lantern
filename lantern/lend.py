"""Lend your Lantern (S13): a family member gets a time-boxed, scoped grant on this Lantern.

The owner says it in words ("Amma may fill forms and read my bills for 1 hour; never the Bank folder"). Muse Spark
turns the words into a Cedar policy and a time limit (structured output, as for S6's rules); Lantern shows the owner
what the engine itself decides for example requests before the grant is saved. Then:
  - The borrower writes (or sends a voice note, transcribed by Muse Voice Transcribe as for the owner) from their own
    WhatsApp/Telegram. Only an active grant for that exact sender lets a message through; anyone else is ignored.
  - Their request runs as a normal task, but before every tool call, in code: the grant must still be active, the tool
    must be one a lent Lantern does at all, private and hidden zones are refused, and the Cedar policy must allow the
    call for every file and site it touches. Then the owner approves that exact call on their phone (red, bound to
    its hash). The tool's own approvals (a submit, a move) still follow.
  - The borrower gets the reply; the owner gets the full receipt.
  - A grant ends on its own at its time (checked on every message and every call, and swept each minute, which stops
    its running tasks), and the owner can revoke it at once.
Grants live in Lantern's own folder (network/grants.json, owner-only).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

from .policy import HIDDEN, PRIVATE, PolicyError

PRINCIPAL = 'Borrower::"borrower"'      # each grant has its own policy, so one name serves every grant
LENDABLE = ("list_folders", "list_dir", "search_files", "read_file", "write_file", "move_file", "organize_files",
            "fill_form", "send_email", "set_reminder", "list_reminders")
FREE = ("ask_reader",)                  # S0's reader: reads what the task already has; no action of its own
PATH_KEYS = ("path", "source", "destination", "folder", "file", "attachments")
MAX_HOURS = 24.0

LEND_PROMPT = """You turn a Lantern owner's words about lending their Lantern to a family member into a Cedar policy
and a time limit. Use exactly these names:
  principal: Borrower::"borrower"
  actions: Action::"list_folders", Action::"list_dir", Action::"search_files", Action::"read_file",
           Action::"write_file", Action::"move_file", Action::"organize_files", Action::"fill_form" (fill a web
           form), Action::"send_email", Action::"set_reminder", Action::"list_reminders"
  resource: Target with attributes kind ("file", "web" or "none"), path (full path), folder (the folder it is in),
            folders (every folder on its path, a set of names), name, ext (".pdf"), host (a web form's site).
"Download bills" means listing, searching and reading files in a bills folder. "Fill forms" means fill_form.
Write only permit and forbid statements for this principal, in this exact form:
  permit(principal == Borrower::"borrower", action == Action::"read_file", resource) when { resource.folder == "Bills" };
Whatever the words don't allow stays denied; "never X" is a forbid. hours: how long the loan lasts (1 if not said, at most 24).
Also give up to 6 example requests that show the rules at work: {"action", "path", "host"} ("" when not used).
Answer only with the JSON object the schema asks for."""
LEND_SCHEMA = {
    "type": "object",
    "properties": {"cedar": {"type": "string"}, "hours": {"type": "number"},
                   "examples": {"type": "array", "items": {"type": "object", "properties": {
                       "action": {"type": "string", "enum": list(LENDABLE)}, "path": {"type": "string"},
                       "host": {"type": "string"}}, "required": ["action", "path", "host"],
                       "additionalProperties": False}}},
    "required": ["cedar", "hours", "examples"], "additionalProperties": False,
}


class LendError(ValueError):
    """A grant can't be made or used; the message is safe to show."""


# ---- the grants, in Lantern's own folder ----------------------------------------------------------------
class Grants:
    def __init__(self, folder: str | Path):
        self.path = Path(folder) / "grants.json"

    def all(self) -> dict[str, dict]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def add(self, name: str, channel: str, address: str, text: str, cedar: str, hours: float,
            now: float | None = None) -> dict:
        now = time.time() if now is None else now
        g = {"id": "g" + secrets.token_hex(4), "name": name, "channel": channel, "address": address, "text": text,
             "cedar": cedar, "created": now, "expires_at": now + hours * 3600, "status": "active", "tasks": []}
        data = self.all()
        data[g["id"]] = g
        self._save(data)
        return g

    def get(self, gid: str) -> dict | None:
        return self.all().get(gid)

    def update(self, gid: str, **fields) -> dict:
        data = self.all()
        if gid not in data:
            raise LendError("there's no such grant")
        data[gid].update(fields)
        self._save(data)
        return data[gid]

    def add_task(self, gid: str, task_id: int) -> None:
        data = self.all()
        data[gid]["tasks"] = (data[gid].get("tasks") or []) + [task_id]
        self._save(data)

    def find(self, channel: str, address: str, now: float | None = None) -> dict | None:
        """The active grant for this sender on this channel, if any."""
        from .channels.base import normalize_id
        who = normalize_id(address)
        for g in self.all().values():
            if g["channel"] == channel and normalize_id(g["address"]) == who and active(g, now):
                return g
        return None

    def due(self, now: float | None = None) -> list[dict]:
        """Active grants whose time is up."""
        now = time.time() if now is None else now
        return [g for g in self.all().values() if g["status"] == "active" and now >= g["expires_at"]]


def active(g: dict, now: float | None = None) -> bool:
    return g.get("status") == "active" and (time.time() if now is None else now) < g["expires_at"]


def until(g: dict) -> str:
    return time.strftime("%H:%M", time.localtime(g["expires_at"]))


# ---- what a call touches, and whether the grant allows it ---------------------------------------------------
def _strings(v: Any) -> list[str]:
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        return [s for x in v for s in _strings(x)]
    if isinstance(v, dict):
        return [s for k, x in v.items() if k in PATH_KEYS for s in _strings(x)]
    return []


def targets(policy, name: str, args: dict) -> list[dict]:
    """Every file and site a tool call touches, as Cedar resource attributes (zone kept for the code check)."""
    out = []
    for key, value in args.items():
        if key in PATH_KEYS or key == "moves":
            for raw in _strings(value) if key == "moves" else _strings({key: value}):
                out.append(_file(policy, raw))
    if name == "fill_form" and args.get("url"):
        from urllib.parse import urlparse
        u = urlparse(str(args["url"]))
        out.append({"kind": "web", "host": (u.hostname or u.scheme or "").lower(), "path": "", "folder": "",
                    "folders": [], "name": "", "ext": ""})
    return out or [{"kind": "none", "path": "", "folder": "", "folders": [], "name": "", "ext": "", "host": ""}]


def _file(policy, raw: str) -> dict:
    try:
        p = policy.resolve(raw) if policy is not None else Path(raw)
        zone = policy.zone_of(p) if policy is not None else ""
    except (PolicyError, OSError, ValueError):
        p, zone = Path(raw), HIDDEN
    return {"kind": "file", "path": str(p), "folder": p.parent.name, "folders": [x for x in p.parent.parts if x not in
            ("/", "\\")], "name": p.name, "ext": p.suffix.lower(), "host": "", "zone": zone}


def check(g: dict, action: str, resource: dict, now: float | None = None) -> tuple[bool, str]:
    """(allowed, why), in code first and then by the grant's Cedar policy."""
    if not active(g, now):
        return False, "the loan has ended" if g.get("status") != "revoked" else "the loan was revoked"
    if action not in LENDABLE:
        return False, f"{action} isn't something a lent Lantern does"
    if resource.get("zone") in (PRIVATE, HIDDEN):
        return False, f"it's in a {resource['zone']} folder, which is never lent"
    from .network import _cedar
    cedarpy = _cedar()
    rid = hashlib.sha256((resource.get("path") or resource.get("host") or "none").encode()).hexdigest()[:16]
    attrs = {k: (list(resource.get(k) or []) if k == "folders" else str(resource.get(k) or ""))
             for k in ("kind", "path", "folder", "folders", "name", "ext", "host")}
    entities = [{"uid": {"__entity": {"type": "Borrower", "id": "borrower"}}, "attrs": {}, "parents": []},
                {"uid": {"__entity": {"type": "Target", "id": rid}}, "attrs": attrs, "parents": []}]
    req = {"principal": PRINCIPAL, "action": f'Action::"{action}"', "resource": f'Target::"{rid}"', "context": {}}
    r = cedarpy.is_authorized(req, g.get("cedar") or "", entities)
    return (True, "the loan allows it") if r.allowed else (False, "the loan doesn't allow it")


def describe(name: str, args: dict, found: list[dict]) -> str:
    what = {"list_folders": "list your folders", "list_dir": "look in a folder", "search_files": "search your files",
            "read_file": "read a file", "write_file": "write a file", "move_file": "move a file",
            "organize_files": "move files", "fill_form": "fill a web form", "send_email": "send an email",
            "set_reminder": "set a reminder", "list_reminders": "list reminders"}.get(name, name)
    where = [r["path"] or r["host"] for r in found if r["kind"] != "none"]
    extra = f" “{str(args.get('query'))[:60]}”" if name == "search_files" and args.get("query") else ""
    return what + extra + (": " + ", ".join(where[:4]) + (" …" if len(where) > 4 else "") if where else "")


class Guard:
    """Installed on a borrowed task's toolbox: every tool call is checked, then approved by the owner."""

    def __init__(self, grants: Grants, gid: str, policy):
        self.grants, self.gid, self.policy = grants, gid, policy
        self.calls: list[dict] = []

    def allow(self, toolbox, name: str, args: dict) -> None:
        from .tools import ToolError
        if name in FREE:
            return
        if name == "ask_user":
            raise ToolError("someone who borrowed this Lantern asked for this: put your question in your final answer "
                            "and they can reply")
        g = self.grants.get(self.gid) or {"status": "revoked", "expires_at": 0}
        found = targets(self.policy, name, args)
        for r in found:
            ok, why = check(g, name, r)
            if not ok:
                self.calls.append({"tool": name, "allowed": False, "why": why})
                raise ToolError(f"not allowed: {why}")
        shown = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
        payload = {"grant": self.gid, "tool": name, "args_sha256": hashlib.sha256(shown.encode()).hexdigest()}
        commit = toolbox._approve("lent_action", f"🔑 {g['name']} (lent until {until(g)}) wants Lantern to "
                                                  f"{describe(name, args, found)}", payload)
        if commit is None:
            self.calls.append({"tool": name, "allowed": False, "why": "the owner said no"})
            raise ToolError("the owner said no to this step")
        if refused := toolbox._commit(commit, payload):
            raise ToolError(refused.get("reason") or "not approved")
        self.calls.append({"tool": name, "allowed": True})


# ---- the owner's words -> a policy and a time limit ---------------------------------------------------------
def compile_grant(client, name: str, text: str, *, memory=None, task_id: int | None = None) -> dict:
    """{"cedar", "hours", "examples": [{..., "allowed": what the engine decides}]} for the owner to look at."""
    from . import models
    from .ladder import RUNGS
    from .network import NetError, _cedar, tidy_cedar
    from .reader import ask_json
    text = " ".join(str(text).split())[:1000]
    if not text:
        raise LendError("say what they may do, and for how long")
    data, _cost, error = ask_json(client, memory=memory, task_id=task_id, rung=RUNGS[3], system=LEND_PROMPT,
                                  user=f"Lending to {name}: {text}", schema=LEND_SCHEMA, name="lend_rules",
                                  kind="compile")
    if data is None:
        raise LendError(f"couldn't turn that into a grant: {error}")
    cedar = tidy_cedar(str(data.get("cedar", "")))
    try:
        _cedar().format_policies(cedar)
    except NetError:
        raise
    except Exception as e:
        raise LendError("the policy Muse Spark wrote doesn't parse; try wording it differently") from e
    if re.search(r'\bprincipal\s*(?:==|in)\s*(?!Borrower::"borrower")\w+::', cedar):
        raise LendError("the policy names someone else")
    hours = hours_of(data.get("hours"))
    trial = {"name": name, "status": "active", "expires_at": time.time() + 60, "cedar": cedar}
    examples = []
    for ex in (data.get("examples") or [])[:6]:
        if ex.get("host"):
            r = {"kind": "web", "host": str(ex["host"]).lower(), "path": "", "folder": "", "folders": [], "name": "",
                 "ext": ""}
        elif ex.get("path"):
            r = _file(None, str(ex["path"]))
            r.pop("zone", None)
        else:
            r = targets(None, "", {})[0]
        ok, _ = check(trial, str(ex.get("action")), r)
        examples.append({**ex, "allowed": ok})       # the engine's decision, not the model's
    return {"name": name, "text": text, "cedar": cedar, "hours": hours, "model": models.badge(models.SPARK),
            "examples": examples}


def hours_of(v: Any) -> float:
    try:
        h = float(v)
    except (TypeError, ValueError):
        return 1.0
    return 1.0 if h != h or h <= 0 else min(MAX_HOURS, max(0.25, h))
