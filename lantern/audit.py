"""The cloud audit log: a record of everything that left the laptop.

Every model call goes through `GuardedClient`, which:
  1. scrubs secrets and ID numbers out of `instructions` and `input` (cloud lane only),
  2. records time, lane, model, endpoint, byte count, SHA-256 of the exact payload
     sent, and how many things were redacted, then
  3. forwards the call.
Calls to the private Glimmer lane are recorded too but are not scrubbed, because
private content is allowed there: lane="local" never leaves the machine,
lane="selfhosted" goes to the user's own server (e.g. Modal) and is shown separately.

The UI's "What left my laptop today" view is `AuditLog.summary(since=today)`.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
import time
from collections import Counter
from pathlib import Path
from typing import Any

from . import models
from .scrubber import DEFAULT, Scrubber

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    task_id INTEGER,
    lane TEXT NOT NULL,          -- cloud | local | selfhosted
    destination TEXT NOT NULL,   -- base URL
    endpoint TEXT NOT NULL,      -- responses | chat.completions | asr.transcribe | ...
    model TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    redactions TEXT NOT NULL,    -- JSON {kind: count}
    preview TEXT NOT NULL        -- first 300 chars of what was sent (already scrubbed)
);
"""
SCRUBBED_FIELDS = ("instructions", "input", "messages")


def canonical_bytes(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def start_of_today() -> float:
    return dt.datetime.combine(dt.date.today(), dt.time()).timestamp()


def _plain(obj: Any) -> Any:
    """SDK objects (pydantic) -> dicts so their strings can be scrubbed."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(exclude_none=True)
    if isinstance(obj, list):
        return [_plain(o) for o in obj]
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    return obj


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def record(self, *, lane: str, destination: str, endpoint: str, model: str, payload: bytes,
               redactions: Counter | dict | None = None, task_id: int | None = None,
               preview: str = "") -> dict:
        row = {
            "ts": time.time(), "task_id": task_id, "lane": lane, "destination": destination,
            "endpoint": endpoint, "model": model, "bytes": len(payload), "sha256": sha256_hex(payload),
            "redactions": json.dumps(dict(redactions or {})), "preview": preview[:300],
        }
        self.db.execute(
            "INSERT INTO audit (ts, task_id, lane, destination, endpoint, model, bytes, sha256, redactions, preview)"
            " VALUES (:ts, :task_id, :lane, :destination, :endpoint, :model, :bytes, :sha256, :redactions, :preview)",
            row,
        )
        self.db.commit()
        return row

    def entries(self, since: float = 0.0, limit: int = 200) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM audit WHERE ts >= ? ORDER BY id DESC LIMIT ?", (since, limit)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["redactions"] = json.loads(d["redactions"])
            out.append(d)
        return out

    def summary(self, since: float | None = None) -> dict:
        """What left the laptop since `since` (default: start of today)."""
        since = start_of_today() if since is None else since
        cloud = {"calls": 0, "bytes": 0, "by_model": {}, "redactions": Counter()}
        local = {"calls": 0, "bytes": 0}
        selfhosted = {"calls": 0, "bytes": 0}
        for e in self.entries(since, limit=100_000):
            if e["lane"] in ("local", "selfhosted"):
                bucket = local if e["lane"] == "local" else selfhosted
                bucket["calls"] += 1
                bucket["bytes"] += e["bytes"]
                continue
            cloud["calls"] += 1
            cloud["bytes"] += e["bytes"]
            m = cloud["by_model"].setdefault(e["model"], {"calls": 0, "bytes": 0})
            m["calls"] += 1
            m["bytes"] += e["bytes"]
            cloud["redactions"].update(e["redactions"])
        cloud["redactions"] = dict(cloud["redactions"])
        return {"since": since, "cloud": cloud, "local": local, "selfhosted": selfhosted}

    def close(self) -> None:
        self.db.close()


class _Endpoint:
    def __init__(self, guard: "GuardedClient", target: Any, endpoint: str):
        self._guard, self._target, self._endpoint = guard, target, endpoint

    def create(self, **kw):
        return self._guard._send(self._target, self._endpoint, kw)


class _Chat:
    def __init__(self, guard: "GuardedClient", inner: Any):
        self.completions = _Endpoint(guard, inner.completions, "chat.completions")


class GuardedClient:
    """Wraps an OpenAI-compatible client. Same `.responses.create` / `.chat.completions.create`
    surface, but every payload is scrubbed (cloud lane) and audited first."""

    def __init__(self, client: Any, audit: AuditLog, *, lane: str = "cloud",
                 destination: str = models.BASE_URL, scrubber: Scrubber = DEFAULT):
        if lane not in ("cloud", "local", "selfhosted"):
            raise ValueError("lane must be 'cloud', 'local' or 'selfhosted'")
        self.inner = client
        self.audit = audit
        self.lane = lane
        self.destination = destination
        self.scrubber = scrubber
        self.task_id: int | None = None
        if hasattr(client, "responses"):
            self.responses = _Endpoint(self, client.responses, "responses")
        if hasattr(client, "chat"):
            self.chat = _Chat(self, client.chat)

    def set_task(self, task_id: int | None) -> None:
        self.task_id = task_id

    def prepare(self, kw: dict) -> tuple[dict, Counter]:
        """Return the payload as it will actually be sent, plus redaction counts."""
        kw = dict(kw)
        found: Counter = Counter()
        if self.lane == "cloud":
            models.check_model(str(kw.get("model", "")))
            for name in SCRUBBED_FIELDS:
                if name in kw:
                    kw[name], _ = self.scrubber.scrub_obj(_plain(kw[name]), found)
        return kw, found

    def _send(self, target: Any, endpoint: str, kw: dict):
        sent, found = self.prepare(kw)
        body = canonical_bytes({k: v for k, v in sent.items() if k != "stream"})
        preview_src = sent.get("input", sent.get("messages", ""))
        preview = preview_src if isinstance(preview_src, str) else json.dumps(preview_src, default=str)
        self.audit.record(lane=self.lane, destination=self.destination, endpoint=endpoint,
                          model=str(sent.get("model", "")), payload=body, redactions=found,
                          task_id=self.task_id, preview=preview)
        return target.create(**sent)
