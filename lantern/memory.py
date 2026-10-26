"""Lantern's memory: what it's doing, what it has done, and what it knows about you.

Everything lives in one local SQLite file. Nothing here leaves the machine.

Three tables:
  tasks  - every goal you give Lantern, with status and a final summary
  steps  - the timeline inside a task (plan, tool calls, results, checks)
  facts  - long-lived things Lantern learned ("manager's email is priya@...")
"""
from __future__ import annotations

import json
import sqlite3
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


class Memory:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ---- tasks -------------------------------------------------------------
    def start_task(self, goal: str) -> Task:
        now = time.time()
        cur = self.db.execute(
            "INSERT INTO tasks (goal, status, created_at, updated_at) VALUES (?, 'active', ?, ?)",
            (goal, now, now),
        )
        self.db.commit()
        return self.get_task(cur.lastrowid)

    def get_task(self, task_id: int) -> Task:
        row = self.db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"no task {task_id}")
        return Task(**dict(row))

    def finish_task(self, task_id: int, status: str, summary: str) -> None:
        if status not in TASK_STATUSES:
            raise ValueError(f"bad status {status!r}")
        self.db.execute(
            "UPDATE tasks SET status = ?, summary = ?, updated_at = ? WHERE id = ?",
            (status, summary, time.time(), task_id),
        )
        self.db.commit()

    def recent_tasks(self, limit: int = 10) -> list[Task]:
        rows = self.db.execute(
            "SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [Task(**dict(r)) for r in rows]

    def active_tasks(self) -> list[Task]:
        rows = self.db.execute(
            "SELECT * FROM tasks WHERE status = 'active' ORDER BY id"
        ).fetchall()
        return [Task(**dict(r)) for r in rows]

    # ---- steps -------------------------------------------------------------
    def log_step(self, task_id: int, kind: str, content) -> None:
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False, default=str)
        self.db.execute(
            "INSERT INTO steps (task_id, kind, content, created_at) VALUES (?, ?, ?, ?)",
            (task_id, kind, content, time.time()),
        )
        self.db.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (time.time(), task_id))
        self.db.commit()

    def steps(self, task_id: int) -> list[dict]:
        rows = self.db.execute(
            "SELECT kind, content, created_at FROM steps WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- facts -------------------------------------------------------------
    def remember(self, key: str, value: str) -> None:
        key = key.strip().lower()
        if not key:
            raise ValueError("fact key is empty")
        self.db.execute(
            "INSERT INTO facts (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, time.time()),
        )
        self.db.commit()

    def forget(self, key: str) -> bool:
        cur = self.db.execute("DELETE FROM facts WHERE key = ?", (key.strip().lower(),))
        self.db.commit()
        return cur.rowcount > 0

    def facts(self) -> dict[str, str]:
        rows = self.db.execute("SELECT key, value FROM facts ORDER BY key").fetchall()
        return {r["key"]: r["value"] for r in rows}

    # ---- context for the model ----------------------------------------------
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

    def close(self) -> None:
        self.db.close()
