"""Lantern's memory: what it's doing, what it has done, and what it knows about you.

Everything lives in one local SQLite file. Nothing here leaves the machine.
With a Cipher (crypto.py), task goals and summaries, step contents and fact values are encrypted
at rest; old plaintext rows are encrypted on first open. Deleting is real: secure_delete overwrites
freed pages and VACUUM rebuilds the file after a forget.

Tables:
  tasks      - every goal you give Lantern, with status and a final summary
  steps      - the timeline inside a task (plan, tool calls, results, checks)
  facts      - long-lived things Lantern learned ("manager's email is priya@...")
  usage      - tokens and USD cost of every model call, per task
  reminders  - plain timers that ping the phone and the desktop (reminders.py)
  task_state - how to pick an unfinished task up again after parking or a restart (scheduler.py)
  ladder_stats - which model rung worked for which kind of step (ladder.py)
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
    status TEXT NOT NULL DEFAULT 'active',   -- active | done | failed | cancelled | interrupted | resumed
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
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    due_at REAL NOT NULL,
    repeat TEXT NOT NULL DEFAULT '',           -- '' | daily | weekly
    status TEXT NOT NULL DEFAULT 'pending',    -- pending | delivered | cancelled
    task_id INTEGER,
    created_at REAL NOT NULL,
    delivered_at REAL
);
CREATE TABLE IF NOT EXISTS private_tasks (     -- tasks that used the private lane: their summaries never
    task_id INTEGER PRIMARY KEY                -- go back to the cloud in the briefing
);
CREATE TABLE IF NOT EXISTS spend (             -- dollars per local day, for the spending limits (budget.py)
    day TEXT PRIMARY KEY,                      -- YYYY-MM-DD; no tasks, no content: kept when you forget things
    cost_usd REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS task_state (        -- what the scheduler needs to resume an unfinished task (scheduler.py)
    task_id INTEGER PRIMARY KEY,
    phase TEXT NOT NULL,                       -- queued | running | parked
    reason TEXT NOT NULL DEFAULT '',           -- parked: waiting_user | waiting_approval | backoff | waiting_browser
    state TEXT NOT NULL,                       -- JSON (encrypted): response id, the turn's calls and results, question
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ladder_stats (      -- model ladder outcomes (ladder.py): kinds and rungs only, no content
    kind TEXT NOT NULL,
    rung INTEGER NOT NULL,
    ok INTEGER NOT NULL,
    cost_usd REAL NOT NULL DEFAULT 0,
    seconds REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
"""

PRIVATE_SUMMARY = "(answered from a private file; the details stay on this computer)"
TASK_STATUSES = {"active", "done", "failed", "cancelled", "interrupted", "resumed"}


@dataclass
class Reminder:
    id: int
    text: str
    due_at: float
    repeat: str
    status: str
    task_id: int | None
    created_at: float
    delivered_at: float | None

    def public(self) -> dict:
        from .reminders import describe
        return {"id": self.id, "text": self.text, "due_at": self.due_at, "due": describe(self.due_at),
                "repeat": self.repeat, "status": self.status}


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


ENCRYPTED = (("tasks", "goal"), ("tasks", "summary"), ("steps", "content"), ("facts", "value"),
             ("reminders", "text"))


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
        self._seed_spend()
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
        key_col = {"tasks": "id", "steps": "id", "facts": "key", "reminders": "id"}
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

    @_locked
    def tasks_with_status(self, status: str, limit: int = 50) -> list[Task]:
        rows = self.db.execute("SELECT * FROM tasks WHERE status = ? ORDER BY id LIMIT ?", (status, limit)).fetchall()
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
        if cost_usd:
            self.db.execute("INSERT INTO spend (day, cost_usd) VALUES (?, ?) "
                            "ON CONFLICT(day) DO UPDATE SET cost_usd = cost_usd + excluded.cost_usd",
                            (time.strftime("%Y-%m-%d"), cost_usd))
        self.db.commit()

    def _seed_spend(self) -> None:
        """A memory.db from before the ledger: start it from the usage rows it still has."""
        if self.db.execute("SELECT COUNT(*) FROM spend").fetchone()[0]:
            return
        self.db.execute("INSERT INTO spend (day, cost_usd) SELECT date(created_at, 'unixepoch', 'localtime'), "
                        "SUM(cost_usd) FROM usage WHERE cost_usd > 0 GROUP BY 1")
        self.db.commit()

    @_locked
    def spent(self, day: str | None = None) -> float:
        """Dollars spent on model calls, ever or on one day (YYYY-MM-DD). Forgetting tasks doesn't lower it:
        the money is gone either way, and the limits must count it."""
        if day is None:
            row = self.db.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM spend").fetchone()
        else:
            row = self.db.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM spend WHERE day = ?", (day,)).fetchone()
        return float(row[0])

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
    def task_usage_by_model(self, task_id: int) -> list[dict]:
        rows = self.db.execute(
            "SELECT model, COUNT(*) AS calls, SUM(input_tokens) AS input_tokens, SUM(cached_tokens) AS cached_tokens,"
            " SUM(output_tokens) AS output_tokens, SUM(cost_usd) AS cost_usd FROM usage WHERE task_id = ?"
            " GROUP BY model ORDER BY model", (task_id,)).fetchall()
        return [dict(r) for r in rows]

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
                  for table in ("steps", "usage", "reminders")}
        self.db.execute("DELETE FROM private_tasks WHERE task_id = ?", (task_id,))
        self.db.execute("DELETE FROM task_state WHERE task_id = ?", (task_id,))
        counts["tasks"] = self.db.execute("DELETE FROM tasks WHERE id = ?", (task_id,)).rowcount
        self._vacuum()
        return counts

    @_locked
    def forget_everything(self) -> dict:
        counts = {table: self.db.execute(f"DELETE FROM {table}").rowcount
                  for table in ("steps", "usage", "tasks", "facts", "reminders")}
        self.db.execute("DELETE FROM private_tasks")
        self.db.execute("DELETE FROM task_state")
        self.db.execute("DELETE FROM ladder_stats")
        self.db.execute("DELETE FROM sqlite_sequence")
        self._vacuum()
        return counts

    # ---- reminders -----------------------------------------------------------
    def _reminder(self, row) -> Reminder:
        d = dict(row)
        d["text"] = self._dec(d["text"])
        return Reminder(**d)

    @_locked
    def add_reminder(self, text: str, due_at: float, repeat: str = "", task_id: int | None = None) -> Reminder:
        cur = self.db.execute(
            "INSERT INTO reminders (text, due_at, repeat, status, task_id, created_at) VALUES (?, ?, ?, 'pending', ?, ?)",
            (self._enc(text), due_at, repeat, task_id, time.time()))
        self.db.commit()
        return self.get_reminder(cur.lastrowid)

    @_locked
    def get_reminder(self, reminder_id: int) -> Reminder:
        row = self.db.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
        if row is None:
            raise KeyError(f"no reminder {reminder_id}")
        return self._reminder(row)

    @_locked
    def reminders(self, status: str = "pending", limit: int = 50) -> list[Reminder]:
        rows = self.db.execute("SELECT * FROM reminders WHERE status = ? ORDER BY due_at LIMIT ?",
                               (status, limit)).fetchall()
        return [self._reminder(r) for r in rows]

    @_locked
    def due_reminders(self, now: float) -> list[Reminder]:
        rows = self.db.execute("SELECT * FROM reminders WHERE status = 'pending' AND due_at <= ? ORDER BY due_at",
                               (now,)).fetchall()
        return [self._reminder(r) for r in rows]

    @_locked
    def reminder_fired(self, reminder_id: int, at: float, next_due: float | None = None) -> None:
        """Delivered: done, or (repeating) moved to its next time."""
        if next_due is None:
            self.db.execute("UPDATE reminders SET status = 'delivered', delivered_at = ? WHERE id = ?",
                            (at, reminder_id))
        else:
            self.db.execute("UPDATE reminders SET due_at = ?, delivered_at = ? WHERE id = ?",
                            (next_due, at, reminder_id))
        self.db.commit()

    @_locked
    def cancel_reminder(self, reminder_id: int) -> bool:
        n = self.db.execute("UPDATE reminders SET status = 'cancelled' WHERE id = ? AND status = 'pending'",
                            (reminder_id,)).rowcount
        self.db.commit()
        return n == 1

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

        upcoming = self.reminders()[:5]
        if upcoming:
            lines.append("\n## Upcoming reminders")
            lines += [f"- #{r.id} {r.public()['due']}{' (' + r.repeat + ')' if r.repeat else ''}: {r.text}"
                      for r in upcoming]

        lines.append("\n## Recently finished")
        finished = [t for t in self.recent_tasks(task_limit) if t.status != "active"]
        private = self._private_task_ids()
        lines += [
            (f"- #{t.id} [{t.status}] {t.goal} -> {PRIVATE_SUMMARY}" if t.id in private else
             f"- #{t.id} [{t.status}] {t.goal} -> {t.summary or ''}".rstrip(" ->"))
            for t in finished
        ] or ["- (none)"]
        return "\n".join(lines)

    # ---- private-lane tasks ---------------------------------------------------
    @_locked
    def mark_private(self, task_id: int) -> None:
        """This task used the private lane: its summary may hold what Glimmer read, so the
        briefing (sent to the cloud with every later request) shows only that it happened."""
        self.db.execute("INSERT OR IGNORE INTO private_tasks (task_id) VALUES (?)", (task_id,))
        self.db.commit()

    @_locked
    def is_private_task(self, task_id: int) -> bool:
        return self.db.execute("SELECT 1 FROM private_tasks WHERE task_id = ?", (task_id,)).fetchone() is not None

    def _private_task_ids(self) -> set[int]:
        return {r[0] for r in self.db.execute("SELECT task_id FROM private_tasks").fetchall()}

    # ---- scheduler state (scheduler.py) ---------------------------------------------
    @_locked
    def save_task_state(self, task_id: int, phase: str, reason: str, state: dict) -> None:
        self.db.execute(
            "INSERT INTO task_state (task_id, phase, reason, state, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET phase = excluded.phase, reason = excluded.reason, "
            "state = excluded.state, updated_at = excluded.updated_at",
            (task_id, phase, reason, self._enc(json.dumps(state, ensure_ascii=False, default=str)), time.time()))
        self.db.commit()

    @_locked
    def load_task_state(self, task_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM task_state WHERE task_id = ?", (task_id,)).fetchone()
        return None if row is None else self._state_row(row)

    @_locked
    def task_states(self) -> list[dict]:
        return [self._state_row(r) for r in self.db.execute("SELECT * FROM task_state ORDER BY task_id").fetchall()]

    def _state_row(self, row) -> dict:
        return {"task_id": row["task_id"], "phase": row["phase"], "reason": row["reason"],
                "state": json.loads(self._dec(row["state"])), "updated_at": row["updated_at"]}

    @_locked
    def delete_task_state(self, task_id: int) -> None:
        self.db.execute("DELETE FROM task_state WHERE task_id = ?", (task_id,))
        self.db.commit()

    # ---- model ladder outcomes (ladder.py) ---------------------------------------------
    @_locked
    def log_ladder(self, kind: str, rung: int, ok: bool, cost_usd: float = 0.0, seconds: float = 0.0) -> None:
        self.db.execute("INSERT INTO ladder_stats (kind, rung, ok, cost_usd, seconds, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)", (kind, rung, int(bool(ok)), cost_usd, seconds, time.time()))
        self.db.commit()

    @_locked
    def ladder_outcomes(self, kind: str | None = None, limit: int = 1000) -> list[dict]:
        """Newest first."""
        if kind is None:
            rows = self.db.execute("SELECT * FROM ladder_stats ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
        else:
            rows = self.db.execute("SELECT * FROM ladder_stats WHERE kind = ? ORDER BY rowid DESC LIMIT ?",
                                   (kind, limit)).fetchall()
        return [dict(r) for r in rows]

    @_locked
    def close(self) -> None:
        self.db.close()
