"""Spending limits, enforced in code before every model call (never only in a prompt).

Three limits, each from the environment (a number in dollars; "off" or 0 turns one off):
  LANTERN_BUDGET_USD        everything this Lantern has spent       (default 150: the hackathon credit)
  LANTERN_DAILY_BUDGET_USD  spent today, local time                  (default off)
  LANTERN_TASK_BUDGET_USD   spent by the running task                (default 3: a loop can't run up the bill)

The check runs inside RetryingClient before the first attempt, so a refused call is never retried, never
leaves the laptop, and never trips the circuit breaker. Spending comes from memory's daily ledger, which
forgetting tasks doesn't lower. Past 80% of the total, Lantern says so once.
Rehearsal (`--demo`) costs $0, so it never trips a limit.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from .memory import Memory
from .reliability import UserFacingError

WARN_AT = 0.8
DEFAULTS = {"total": 150.0, "daily": None, "task": 3.0}
ENV = {"total": "LANTERN_BUDGET_USD", "daily": "LANTERN_DAILY_BUDGET_USD", "task": "LANTERN_TASK_BUDGET_USD"}


def usd(x: float) -> str:
    """$150.00, $3.00, but $0.002 for tiny limits (never a misleading "$0.00")."""
    return f"${x:.2f}" if x >= 0.995 or x == 0 or round(x, 2) == x else f"${x:.4f}".rstrip("0")


class BudgetError(UserFacingError):
    """A spending limit stopped a model call. The message says which, and how to raise it."""


def _limit(raw: str | None, default: float | None) -> float | None:
    if raw is None or not raw.strip():
        return default
    if raw.strip().lower() in ("off", "none", "no", "0"):
        return None
    try:
        value = float(raw.strip().lstrip("$"))
    except ValueError:
        return default   # a typo must not switch a limit off; `lantern --doctor` points it out
    return value if value > 0 else None


@dataclass(frozen=True)
class Limits:
    total: float | None = DEFAULTS["total"]
    daily: float | None = DEFAULTS["daily"]
    task: float | None = DEFAULTS["task"]

    @classmethod
    def from_env(cls, env: dict) -> "Limits":
        return cls(**{k: _limit(env.get(name), DEFAULTS[k]) for k, name in ENV.items()})

    def problems(self, env: dict) -> list[str]:
        """Values in the environment that couldn't be read (they fall back to the default)."""
        out = []
        for k, name in ENV.items():
            raw = (env.get(name) or "").strip()
            if raw and raw.lower() not in ("off", "none", "no", "0"):
                try:
                    float(raw.lstrip("$"))
                except ValueError:
                    out.append(f"{name}={raw!r} isn't a number")
        return out


SCHEDULED_TASK_DEFAULT = 0.10   # a loop's run costs less than an interactive task ($3)


def scheduled_task_budget(env: dict) -> float | None:
    """The per-run budget for scheduled pack runs: LANTERN_TASK_BUDGET_USD when set,
    else $0.10. "off"/"0" means no cap, like the other limits."""
    return _limit(env.get(ENV["task"]), SCHEDULED_TASK_DEFAULT)


class BudgetGuard:
    def __init__(self, memory: Memory, limits: Limits, current_task: Callable[[], int | None] = lambda: None,
                 on_event: Callable[[dict], None] | None = None):
        self.memory, self.limits = memory, limits
        self.current_task = current_task
        self.on_event = on_event or (lambda e: None)
        self.overrides: dict[int, float | None] = {}   # per-task caps (scheduled runs); cleared when they settle
        self._warned = False

    def set_task_budget(self, task_id: int, cap_usd: float | None) -> None:
        """Cap one task's spending (a scheduled run). Cleared with clear_task_budget."""
        self.overrides[task_id] = cap_usd

    def clear_task_budget(self, task_id: int) -> None:
        self.overrides.pop(task_id, None)

    def check(self) -> None:
        """Raise BudgetError if any limit is reached; otherwise let the call through."""
        lim = self.limits
        if lim.total is not None:
            spent = self.memory.spent()
            if spent >= lim.total:
                raise BudgetError(f"Lantern has spent {usd(spent)} of its {usd(lim.total)} limit, so it stopped before "
                                  f"calling the model. Raise {ENV['total']} in .env to go on.")
            if spent >= WARN_AT * lim.total and not self._warned:
                self._warned = True
                self.on_event({"type": "budget", "level": "warn", "spent_usd": round(spent, 4),
                               "limit_usd": lim.total})
        if lim.daily is not None:
            today = self.memory.spent(time.strftime("%Y-%m-%d"))
            if today >= lim.daily:
                raise BudgetError(f"Today's spending reached the {usd(lim.daily)} daily limit ({ENV['daily']}); "
                                  "Lantern will call the model again tomorrow, or raise the limit in .env.")
        task_id = self.current_task()
        cap = self.overrides[task_id] if task_id in self.overrides else lim.task
        if cap is not None and task_id is not None:
            cost = self.memory.task_usage(task_id)["cost_usd"]
            if cost >= cap:
                raise BudgetError(f"This task reached its {usd(cap)} limit ({ENV['task']}) and was stopped so it "
                                  "can't run up the bill. Try again, or raise the limit in .env.")

    def status(self) -> dict:
        spent, today = self.memory.spent(), self.memory.spent(time.strftime("%Y-%m-%d"))
        lim = self.limits
        return {"spent_usd": round(spent, 4), "today_usd": round(today, 4),
                "total_usd": lim.total, "daily_usd": lim.daily, "task_usd": lim.task,
                "left_usd": None if lim.total is None else round(max(0.0, lim.total - spent), 4)}
