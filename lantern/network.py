"""Lantern Network (S6): two Lanterns ask each other for things with signed messages; each owner decides.

  Keys       an Ed25519 key per Lantern (~/.lantern/network/key.pem, file mode 600; the cryptography library).
             Your card is a name and the public key; contacts are an allowlist of cards
             (~/.lantern/network/contacts.json), each with the channel address its Lantern listens on.
  Messages   "🏮LN1 <payload>.<signature>" as text over the channel layer: base64url of canonical JSON
             {v, id, ts, from, to, kind, body}. Accepted only from a contact, signed by that contact's key,
             addressed to this Lantern, under 10 minutes old and never seen before (replays are dropped).
  Kinds      ask_file {query}        -> reply {ok, name, text, sha256}: one text file, scrubbed, 32 KB at most
             share_routine {contract} -> imported, and trusted only once it verifies here
             reply {re, ok, ...}
  Rules      per contact, in the owner's own words. Muse Spark turns them into a Cedar policy (structured output)
             and suggests example requests; Lantern runs those examples through the engine and shows the owner what
             the engine actually decides. cedarpy checks every request. Code adds what no policy can lift: private and
             hidden zones are never shared, and every allowed request still needs the owner's own approval.
  Receipts   each side keeps a task per request: who asked, what the rules said, what the owner decided, and what
             left or arrived (name, bytes, sha256).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .scrubber import DEFAULT as SCRUBBER

PREFIX = "🏮LN1 "
MAX_AGE = 600                # seconds a message stays valid
MAX_TEXT = 32 * 1024         # the most a shared file may hold (text only)
TEXT_EXTS = {".txt", ".md", ".csv", ".json", ".log", ".ics", ".eml", ".yaml", ".yml"}
ACTIONS = ("ask_file", "share_routine")

RULES_PROMPT = """You turn a Lantern owner's sharing rules for one contact into a Cedar policy.
The rules are about what that contact's Lantern may ask for. Use exactly these names:
  principal: Contact::"{fp}"
  actions: Action::"ask_file", Action::"share_routine"
  resources: File with attributes folder (the folder's name, e.g. "Bills"), path (its full path), ext (".pdf"),
             name; Routine with attribute name.
Write only permit and forbid statements for this principal, in this exact form:
  permit(principal == Contact::"{fp}", action == Action::"ask_file", resource) when {{ resource.folder == "Bills" }};
Anything the rules don't allow stays denied.
Also give up to 6 example requests that show the rules at work: {{"action", "folder", "name"}}.
Answer only with the JSON object the schema asks for."""
RULES_SCHEMA = {
    "type": "object",
    "properties": {"cedar": {"type": "string"},
                   "examples": {"type": "array", "items": {"type": "object", "properties": {
                       "action": {"type": "string", "enum": list(ACTIONS)}, "folder": {"type": "string"},
                       "name": {"type": "string"}}, "required": ["action", "folder", "name"],
                       "additionalProperties": False}}},
    "required": ["cedar", "examples"], "additionalProperties": False,
}


class NetError(ValueError):
    """A network message or request can't be accepted; the message is safe to show."""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


# ---- keys and contacts -------------------------------------------------------------------------
class Identity:
    def __init__(self, key, name: str):
        self.key, self.name = key, name
        from cryptography.hazmat.primitives import serialization
        self.public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.fp = fingerprint(self.public)

    @classmethod
    def load_or_create(cls, folder: str | Path, name: str = "Lantern") -> "Identity":
        """Lantern's own key, in its own folder (not a user zone)."""
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        folder = Path(folder)
        path = folder / "key.pem"
        if path.exists():
            key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        else:
            folder.mkdir(parents=True, exist_ok=True)
            key = Ed25519PrivateKey.generate()
            path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
            os.chmod(path, 0o600)
        return cls(key, name)

    def card(self) -> dict:
        return {"name": self.name, "key": _b64(self.public), "fp": self.fp}

    def sign(self, data: bytes) -> bytes:
        return self.key.sign(data)


def fingerprint(public: bytes) -> str:
    return hashlib.sha256(public).hexdigest()[:16]


def verify(public_b64: str, data: bytes, sig: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        Ed25519PublicKey.from_public_bytes(_unb64(public_b64)).verify(sig, data)
        return True
    except (InvalidSignature, ValueError):
        return False


class Contacts:
    """The allowlist: cards Lantern accepts messages from, with where each one listens and the owner's rules."""

    def __init__(self, folder: str | Path):
        self.path = Path(folder) / "contacts.json"
        self._lock = threading.Lock()

    def all(self) -> dict[str, dict]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        os.chmod(self.path, 0o600)

    def add(self, card: dict, channel: str, address: str) -> dict:
        key, name = str(card.get("key", "")), " ".join(str(card.get("name", "")).split())[:40]
        try:
            raw = _unb64(key)
        except ValueError as e:
            raise NetError("that card's key isn't readable") from e
        if len(raw) != 32 or not name:
            raise NetError("a card needs a name and a 32-byte Ed25519 key")
        fp = fingerprint(raw)
        if card.get("fp") and card["fp"] != fp:
            raise NetError("the card's fingerprint doesn't match its key")
        with self._lock:
            data = self.all()
            old = data.get(fp, {})
            data[fp] = {"name": name, "key": key, "fp": fp, "channel": channel, "address": str(address),
                        "rules": old.get("rules", {})}
            self._save(data)
        return data[fp]

    def get(self, fp: str) -> dict | None:
        return self.all().get(fp)

    def find(self, name_or_fp: str) -> dict | None:
        q = str(name_or_fp).strip().lower()
        return next((c for c in self.all().values() if c["fp"] == q or c["name"].lower() == q), None)

    def set_rules(self, fp: str, rules: dict) -> None:
        with self._lock:
            data = self.all()
            if fp not in data:
                raise NetError("not a contact")
            data[fp]["rules"] = rules
            self._save(data)


# ---- messages ----------------------------------------------------------------------------------------
def seal(me: Identity, to_fp: str, kind: str, body: dict, *, now: float | None = None) -> tuple[str, dict]:
    env = {"v": 1, "id": uuid.uuid4().hex, "ts": int(now if now is not None else time.time()), "from": me.fp,
           "to": to_fp, "kind": kind, "body": body}
    data = canonical(env)
    return PREFIX + _b64(data) + "." + _b64(me.sign(data)), env


class Inbox:
    """Opens messages: known sender, good signature, for me, fresh, never seen."""

    def __init__(self, me: Identity, contacts: Contacts):
        self.me, self.contacts = me, contacts
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def open(self, text: str, *, now: float | None = None) -> tuple[dict, dict]:
        now = time.time() if now is None else now
        if not text.startswith(PREFIX):
            raise NetError("not a Lantern Network message")
        try:
            payload, sig = text[len(PREFIX):].strip().split(".", 1)
            data, signature = _unb64(payload), _unb64(sig)
            env = json.loads(data)
        except (ValueError, TypeError) as e:
            raise NetError("the message is damaged") from e
        contact = self.contacts.get(str(env.get("from", "")))
        if contact is None:
            raise NetError("the sender isn't one of your contacts")
        if not verify(contact["key"], data, signature) or canonical(env) != data:
            raise NetError("the signature doesn't match the contact's key")
        if env.get("to") != self.me.fp:
            raise NetError("the message isn't addressed to this Lantern")
        if not isinstance(env.get("ts"), int) or abs(now - env["ts"]) > MAX_AGE:
            raise NetError("the message is too old (or from the future)")
        with self._lock:
            self._seen = {k: t for k, t in self._seen.items() if now - t < 2 * MAX_AGE}
            if env.get("id") in self._seen:
                raise NetError("the same message arrived twice; the copy was dropped")
            self._seen[str(env.get("id"))] = now
        if env.get("kind") not in ACTIONS + ("reply",):
            raise NetError(f"unknown request {env.get('kind')!r}")
        return contact, env


# ---- rules: owner's words -> Cedar policy, checked by cedarpy -----------------------------------------
def _cedar():
    try:
        import cedarpy
    except ImportError as e:
        raise NetError("the rules engine isn't installed (pip install cedarpy); until it is, every request is "
                       "refused") from e
    return cedarpy


def tidy_cedar(cedar: str) -> str:
    """Seen live: Muse Spark wrote `principal is Borrower::"borrower"`. Cedar's `is` takes a type, so that doesn't
    parse; an equality with that one entity is what was meant. Nothing else is rewritten."""
    return re.sub(r'\bprincipal\s+is\s+([A-Z]\w*::"[^"]*")', r"principal == \1", cedar.strip())


def check(contact: dict, action: str, resource: dict) -> tuple[bool, str]:
    """(allowed, why). Private and hidden zones are refused in code before any policy is consulted."""
    if resource.get("zone") in ("private", "hidden"):
        return False, f"it's in a {resource['zone']} folder, which is never shared"
    policy = (contact.get("rules") or {}).get("cedar", "")
    if not policy.strip():
        return False, f"you haven't set rules for {contact['name']} yet"
    cedarpy = _cedar()
    kind = "Routine" if action == "share_routine" else "File"
    rid = str(resource.get("name") or "x")
    entities = [{"uid": {"__entity": {"type": "Contact", "id": contact["fp"]}}, "attrs": {}, "parents": []},
                {"uid": {"__entity": {"type": kind, "id": rid}},
                 "attrs": {k: str(v) for k, v in resource.items() if k in ("folder", "path", "ext", "name", "zone")},
                 "parents": []}]
    req = {"principal": f'Contact::"{contact["fp"]}"', "action": f'Action::"{action}"',
           "resource": f'{kind}::"{rid}"', "context": {}}
    r = cedarpy.is_authorized(req, policy, entities)
    return (True, "your rules allow it") if r.allowed else (False, f"your rules for {contact['name']} don't allow it")


def compile_rules(client, contact: dict, text: str, *, memory=None, task_id: int | None = None) -> dict:
    """The owner's words -> {"text", "cedar", "examples": [{..., "allowed": what the engine decides}]}."""
    from . import models
    from .ladder import RUNGS
    from .reader import ask_json
    text = " ".join(str(text).split())[:1000]
    if not text:
        raise NetError("write the rules in a sentence or two")
    data, _cost, error = ask_json(client, memory=memory, task_id=task_id, rung=RUNGS[3],
                                  system=RULES_PROMPT.format(fp=contact["fp"]), user=text, schema=RULES_SCHEMA,
                                  name="network_rules", kind="compile")
    if data is None:
        raise NetError(f"couldn't turn those rules into a policy: {error}")
    cedar = tidy_cedar(str(data.get("cedar", "")))
    cedarpy = _cedar()
    try:
        cedarpy.format_policies(cedar)
    except Exception as e:
        raise NetError("the policy Muse Spark wrote doesn't parse; try wording the rules differently") from e
    if re.search(r"\bprincipal\s*(?:==|in)\s*Contact::\"(?!" + re.escape(contact["fp"]) + r")", cedar):
        raise NetError("the policy names someone else")
    out = {"text": text, "cedar": cedar, "model": models.badge(models.SPARK), "examples": []}
    trial = {**contact, "rules": {"cedar": cedar}}
    for ex in (data.get("examples") or [])[:6]:
        res = {"folder": ex.get("folder", ""), "name": ex.get("name", ""), "path": ex.get("folder", ""),
               "ext": Path(str(ex.get("name", ""))).suffix.lower(), "zone": "open"}
        ok, _ = check(trial, ex.get("action", "ask_file"), res)
        out["examples"].append({**ex, "allowed": ok})      # what the engine decides, not what the model said
    return out


# ---- what a request may take ---------------------------------------------------------------------------
def find_file(policy, query: str) -> Path | None:
    """The visible text file whose name best matches the request (private and hidden zones never listed)."""
    words = {w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2}
    best, score = None, 0
    for root in policy.roots:
        for full, zone in policy.walk(root, {".git", "node_modules", "__pycache__"}):
            if zone in ("private", "hidden") or full.suffix.lower() not in TEXT_EXTS:
                continue
            s = len(words & set(re.findall(r"[a-z0-9]+", full.stem.lower())))
            if s > score:
                best, score = full, s
    return best


def file_resource(policy, path: Path) -> dict:
    return {"folder": path.parent.name, "path": str(path.parent), "ext": path.suffix.lower(), "name": path.name,
            "zone": policy.zone_of(path)}


def shareable_text(policy, path: Path) -> tuple[str, dict]:
    """The file's text as it may leave: read through policy.py (never a private zone), scrubbed, 32 KB at most."""
    text = policy.read_text(path, limit=MAX_TEXT + 1)
    if len(text.encode()) > MAX_TEXT:
        raise NetError(f"{path.name} is over 32 KB; only small text files are shared")
    clean = SCRUBBER.scrub(text)
    return clean.text, dict(clean.redactions)


def shareable_contract(contract: dict) -> dict:
    """A routine as it may leave: no source (task ids, costs), no file defaults, values already placeholders."""
    c = json.loads(json.dumps(contract))
    c.pop("source", None)
    for p in c.get("params") or []:
        if p.get("type") in ("path", "secret"):
            p.pop("default", None)
    return c
