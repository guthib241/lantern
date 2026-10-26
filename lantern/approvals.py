"""The approval bus: nothing that changes the world happens without a yes.

Each approval is
  - bound to a SHA-256 of the exact payload (path + content hash, recipient +
    subject + attachment hashes, form field values...). If anything changes
    after the user said yes, the hash no longer matches and the action is refused,
  - single use: approving once lets the action run once,
  - short lived: it expires 10 minutes after it was requested,
  - protected by a random nonce, so only the card/button that was shown can answer.

The agent runs in a worker thread and blocks in `wait()` while the user decides on
the desktop, in WhatsApp or in the terminal. Listeners get every new request and
decision (that's how the server pushes approval cards to the UI and channels).
"""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass, field, fields
from typing import Any, Callable

EXPIRY_SECONDS = 10 * 60
PENDING, APPROVED, DENIED, EXPIRED, USED = "pending", "approved", "denied", "expired", "used"


class ApprovalError(Exception):
    """An approval could not be used (wrong nonce, expired, payload changed, reused...)."""


def payload_hash(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


@dataclass
class Approval:
    id: str
    nonce: str
    action: str
    summary: str
    payload: dict
    payload_hash: str
    created_at: float
    expires_at: float
    tainted: bool = False
    status: str = PENDING
    decided_by: str | None = None
    decided_at: float | None = None
    _event: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)

    def public(self, include_nonce: bool = False) -> dict:
        d = {f.name: getattr(self, f.name) for f in fields(self) if not f.name.startswith("_")}
        if not include_nonce:
            d.pop("nonce")
        return d


Listener = Callable[[str, Approval], None]  # (event, approval); event in requested|decided|used|expired


class ApprovalBus:
    def __init__(self, expiry: float = EXPIRY_SECONDS, clock: Callable[[], float] = time.time):
        self.expiry = expiry
        self.clock = clock
        self._items: dict[str, Approval] = {}
        self._lock = threading.Lock()
        self._listeners: list[Listener] = []

    def subscribe(self, fn: Listener) -> None:
        self._listeners.append(fn)

    def _emit(self, event: str, a: Approval) -> None:
        for fn in list(self._listeners):
            try:
                fn(event, a)
            except Exception:  # a broken UI listener must never block an approval
                pass

    # ---- lifecycle -----------------------------------------------------------
    def request(self, action: str, summary: str, payload: dict, tainted: bool = False) -> Approval:
        now = self.clock()
        a = Approval(
            id=secrets.token_hex(4), nonce=secrets.token_urlsafe(16), action=action, summary=summary,
            payload=payload, payload_hash=payload_hash(payload), created_at=now,
            expires_at=now + self.expiry, tainted=tainted,
        )
        with self._lock:
            self._items[a.id] = a
        self._emit("requested", a)
        return a

    def get(self, approval_id: str) -> Approval:
        with self._lock:
            a = self._items.get(approval_id)
        if a is None:
            raise ApprovalError(f"no approval {approval_id}")
        self._expire_if_due(a)
        return a

    def pending(self) -> list[Approval]:
        with self._lock:
            items = list(self._items.values())
        for a in items:
            self._expire_if_due(a)
        return [a for a in items if a.status == PENDING]

    def _expire_if_due(self, a: Approval) -> None:
        if a.status in (PENDING, APPROVED) and self.clock() > a.expires_at:
            a.status = EXPIRED
            a._event.set()
            self._emit("expired", a)

    def decide(self, approval_id: str, nonce: str, approve: bool, *, by: str = "user",
               expected_hash: str | None = None) -> Approval:
        """Answer an approval. `nonce` must be the one shown on the card/button.
        `expected_hash` (optional) lets a channel prove it showed this exact payload."""
        a = self.get(approval_id)
        if not secrets.compare_digest(a.nonce, nonce or ""):
            raise ApprovalError("this approval link is not valid")
        if a.status != PENDING:
            raise ApprovalError(f"approval {a.id} is already {a.status}")
        if expected_hash is not None and expected_hash != a.payload_hash:
            raise ApprovalError("the action changed since it was shown; ask again")
        a.status = APPROVED if approve else DENIED
        a.decided_by, a.decided_at = by, self.clock()
        a._event.set()
        self._emit("decided", a)
        return a

    def wait(self, approval_id: str, timeout: float | None = None) -> bool:
        """Block until decided or expired. True only if approved (and still valid)."""
        a = self.get(approval_id)
        remaining = max(0.0, a.expires_at - self.clock())
        a._event.wait(remaining if timeout is None else min(timeout, remaining))
        self._expire_if_due(a)
        return a.status == APPROVED

    def consume(self, approval_id: str, payload: dict) -> None:
        """Call right before acting. Refuses unless approved, unexpired, unused and the
        payload about to be executed hashes to exactly what was approved."""
        a = self.get(approval_id)
        if a.status != APPROVED:
            raise ApprovalError(f"approval {a.id} is {a.status}, not approved")
        if payload_hash(payload) != a.payload_hash:
            raise ApprovalError("the action changed after it was approved; it needs a new approval")
        a.status = USED
        self._emit("used", a)

    def cancel_all(self, reason: str = "cancelled") -> int:
        """Kill switch: deny everything pending."""
        n = 0
        for a in self.pending():
            a.status, a.decided_by, a.decided_at = DENIED, reason, self.clock()
            a._event.set()
            self._emit("decided", a)
            n += 1
        return n


# ---- gates: how tools ask -----------------------------------------------------
class Gate:
    """What a tool calls. Returns a callable `commit(payload)` on yes, or None on no."""

    def ask(self, action: str, summary: str, payload: dict, tainted: bool = False) -> Callable[[dict], None] | None:
        raise NotImplementedError


class CallbackGate(Gate):
    """Synchronous yes/no (terminal prompt, tests). Still binds the payload hash."""

    def __init__(self, fn: Callable[[str], bool], bus: ApprovalBus | None = None):
        self.fn = fn
        self.bus = bus or ApprovalBus()

    def ask(self, action, summary, payload, tainted=False):
        a = self.bus.request(action, summary, payload, tainted)
        ok = bool(self.fn(summary))
        self.bus.decide(a.id, a.nonce, ok, by="callback")
        return (lambda final: self.bus.consume(a.id, final)) if ok else None


class BusGate(Gate):
    """Asynchronous: post to the bus and wait for the desktop/WhatsApp to answer."""

    def __init__(self, bus: ApprovalBus, timeout: float | None = None):
        self.bus = bus
        self.timeout = timeout

    def ask(self, action, summary, payload, tainted=False):
        a = self.bus.request(action, summary, payload, tainted)
        if not self.bus.wait(a.id, self.timeout):
            if self.bus.get(a.id).status == PENDING:  # timed out waiting; don't leave it open
                self.bus.decide(a.id, a.nonce, False, by="timeout")
            return None
        return lambda final: self.bus.consume(a.id, final)


def as_gate(approve: Gate | Callable[[str], bool]) -> Gate:
    return approve if isinstance(approve, Gate) else CallbackGate(approve)
