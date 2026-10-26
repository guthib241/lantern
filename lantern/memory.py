"""Lantern's memory: what it's doing, what it has done, and what it knows about you.

Everything lives in one local SQLite file. Nothing here leaves the machine.
With a Cipher (crypto.py), task goals and summaries, step contents and fact values are encrypted
at rest; old plaintext rows are encrypted on first open. Deleting is real: secure_delete overwrites
freed pages and VACUUM rebuilds the file after a forget.

Three tables:
  tasks  - every goal you give Lantern, with status and a final summary
  steps  - the timeline inside a task (plan, tool calls, results, checks)
  facts  - long-lived things Lantern learned ("manager's email is priya@...")
  usage  - tokens and USD cost of every model call, per task
"""
from __future__ import annotations

import functools
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',   -- active | done | failed | cancelled
    summary TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    kind TEXT NOT NULL,      -- plan | tool_call | tool_result | check | note | reply
    content TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER REFERENCES tasks(id),   -- NULL for calls outside a task
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS facts (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""

TASK_STATUSES = {"active", "done", "failed", "cancelled"}


@dataclass
class Task:
    id: int
    goal: str
    status: str
    summary: str | None
    created_at: float
    updated_at: float


def _locked(fn):
    """Serialize access: the server's agent thread and request threads share one connection."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)
    return wrapper


ENCRYPTED = (("tasks", "goal"), ("tasks", "summary"), ("steps", "content"), ("facts", "value"))


class Memory:
    def __init__(self, path: str | Path, cipher=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.cipher = cipher
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA secure_delete = ON")
        self.db.executescript(SCHEMA)
        self.db.commit()
        if cipher is not None:
            self._encrypt_old_rows()

    # ---- encryption at rest --------------------------------------------------
    def _enc(self, text):
        return self.cipher.encrypt(text) if self.cipher is not None else text

    def _dec(self, text):
        return self.cipher.decrypt(text) if self.cipher is not None else text

    def _task(self, row) -> Task:
        d = dict(row)
        d["goal"], d["summary"] = self._dec(d["goal"]), self._dec(d["summary"])
        return Task(**d)

    @_locked
    def _encrypt_old_rows(self) -> int:
        """One-time migration: encrypt rows written before encryption was on."""
        from .crypto import PREFIX
        n = 0
        key_col = {"tasks": "id", "steps": "id", "facts": "key"}
        for table, col in ENCRYPTED:
            rows = self.db.execute(f"SELECT {key_col[table]} AS k, {col} AS v FROM {table} "
                                   f"WHERE {col} IS NOT NULL AND {col} NOT LIKE ?", (PREFIX + "%",)).fetchall()
            for r in rows:
                self.db.execute(f"UPDATE {table} SET {col} = ? WHERE {key_col[table]} = ?", (self._enc(r["v"]), r["k"]))
                n += 1
        self.db.commit()
        if n:
            self._vacuum()
        return n

    def _vacuum(self) -> None:
        self.db.commit()
        self.db.execute("VACUUM")

    # ---- tasks -------------------------------------------------------------
    @_locked
    def start_task(self, goal: str) -> Task:
        now = time.time()
        cur = self.db.execute(
            "INSERT INTO tasks (goal, status, created_at, updated_at) VALUES (?, 'active', ?, ?)",
            (self._enc(goal), now, now),
        )
        self.db.commit()
        return self.get_task(cur.lastrowid)

    @_locked
    def get_task(self, task_id: int) -> Task:
        row = self.db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"no task {task_id}")
        return self._task(row)

    @_locked
    def finish_task(self, task_id: int, status: str, summary: str) -> None:
        if status not in TASK_STATUSES:
            raise ValueError(f"bad status {status!r}")
        self.db.execute(
            "UPDATE tasks SET status = ?, summary = ?, updated_at = ? WHERE id = ?",
            (status, self._enc(summary), time.time(), task_id),
        )
        self.db.commit()

    @_locked
    def recent_tasks(self, limit: int = 10) -> list[Task]:
        rows = self.db.execute(
            "SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._task(r) for r in rows]

    @_locked
    def active_tasks(self) -> list[Task]:
        rows = self.db.execute(
            "SELECT * FROM tasks WHERE status = 'active' ORDER BY id"
        ).fetchall()
        return [self._task(r) for r in rows]

    # ---- steps -------------------------------------------------------------
    @_locked
    def log_step(self, task_id: int, kind: str, content) -> None:
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False, default=str)
        self.db.execute(
            "INSERT INTO steps (task_id, kind, content, created_at) VALUES (?, ?, ?, ?)",
            (task_id, kind, self._enc(content), time.time()),
        )
        self.db.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (time.time(), task_id))
        self.db.commit()

    @_locked
    def steps(self, task_id: int) -> list[dict]:
        rows = self.db.execute(
            "SELECT kind, content, created_at FROM steps WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
        return [{**dict(r), "content": self._dec(r["content"])} for r in rows]

    # ---- usage / cost --------------------------------------------------------
    @_locked
    def log_usage(self, task_id: int | None, model: str, input_tokens: int = 0,
                  cached_tokens: int = 0, output_tokens: int = 0, cost_usd: float = 0.0) -> None:
        self.db.execute(
            "INSERT INTO usage (task_id, model, input_tokens, cached_tokens, output_tokens, cost_usd, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, model, input_tokens, cached_tokens, output_tokens, cost_usd, time.time()),
        )
        self.db.commit()

    @_locked
    def task_usage(self, task_id: int) -> dict:
        """Totals for one task: calls, tokens, cost."""
        row = self.db.execute(
            "SELECT COUNT(*) AS calls, COALESCE(SUM(input_tokens),0) AS input_tokens,"
            " COALESCE(SUM(cached_tokens),0) AS cached_tokens,"
            " COALESCE(SUM(output_tokens),0) AS output_tokens,"
            " COALESCE(SUM(cost_usd),0) AS cost_usd FROM usage WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        return dict(row)

    @_locked
    def usage_by_model(self, since: float = 0.0) -> list[dict]:
        rows = self.db.execute(
            "SELECT model, COUNT(*) AS calls, SUM(input_tokens) AS input_tokens,"
            " SUM(cached_tokens) AS cached_tokens, SUM(output_tokens) AS output_tokens,"
            " SUM(cost_usd) AS cost_usd FROM usage WHERE created_at >= ? GROUP BY model ORDER BY model",
            (since,),
        ).fetchall()
        return [dict(r) for r in rows]

    @_locked
    def total_cost(self) -> float:
        return float(self.db.execute("SELECT COALESCE(SUM(cost_usd),0) FROM usage").fetchone()[0])

    # ---- facts -------------------------------------------------------------
    @_locked
    def remember(self, key: str, value: str) -> None:
        key = key.strip().lower()
        if not key:
            raise ValueError("fact key is empty")
        self.db.execute(
            "INSERT INTO facts (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, self._enc(value), time.time()),
        )
        self.db.commit()

    @_locked
    def forget(self, key: str) -> bool:
        """Really forget a fact: the row is deleted, overwritten (secure_delete) and the file rebuilt."""
        cur = self.db.execute("DELETE FROM facts WHERE key = ?", (key.strip().lower(),))
        self._vacuum()
        return cur.rowcount > 0

    @_locked
    def facts(self) -> dict[str, str]:
        rows = self.db.execute("SELECT key, value FROM facts ORDER BY key").fetchall()
        return {r["key"]: self._dec(r["value"]) for r in rows}

    # ---- forgetting ------------------------------------------------------------
    @_locked
    def forget_task(self, task_id: int) -> dict:
        """Delete a task with its timeline and usage rows, then rebuild the file."""
        counts = {table: self.db.execute(f"DELETE FROM {table} WHERE task_id = ?", (task_id,)).rowcount
                  for table in ("steps", "usage")}
        counts["tasks"] = self.db.execute("DELETE FROM tasks WHERE id = ?", (task_id,)).rowcount
        self._vacuum()
        return counts

    @_locked
    def forget_everything(self) -> dict:
        counts = {table: self.db.execute(f"DELETE FROM {table}").rowcount for table in ("steps", "usage", "tasks", "facts")}
        self.db.execute("DELETE FROM sqlite_sequence")
        self._vacuum()
        return counts

    # ---- context for the model ----------------------------------------------
    @_locked
    def context_block(self, task_limit: int = 8) -> str:
        """A compact, human-readable briefing injected into every model call,
        so Lantern always knows what it knows and what it has done."""
        lines = ["## What you know about the user"]
        facts = self.facts()
        lines += [f"- {k}: {v}" for k, v in facts.items()] or ["- (nothing yet)"]

        lines.append("\n## Tasks still in progress")
        active = self.active_tasks()
        lines += [f"- #{t.id}: {t.goal}" for t in active] or ["- (none)"]

        lines.append("\n## Recently finished")
        finished = [t for t in self.recent_tasks(task_limit) if t.status != "active"]
        lines += [
            f"- #{t.id} [{t.status}] {t.goal} -> {t.summary or ''}".rstrip(" ->")
            for t in finished
        ] or ["- (none)"]
        return "\n".join(lines)

    @_locked
    def close(self) -> None:
        self.db.close()
