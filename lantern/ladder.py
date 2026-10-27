"""Model ladder and learning router (S3): the cheapest rung that does each kind of step well.

Rungs, cheapest first:
  0 routine     a compiled routine (S4): no model at all
  1 rules       Lantern's own code (moves, writes, reminders, lookups): no model
  2 local       Muse Glimmer on this computer: private content, classification
  3 spark-low   muse-spark-1.3, reasoning effort low
  4 spark-high  muse-spark-1.3, reasoning effort high
  5 wonder      muse-wonder-1.0: computer use, long or stuck tasks

Each step has a kind. The rule is simple and explainable:
  - Pick the lowest rung that can do this kind of step and whose recent success rate is at least 0.8,
    within the task's remaining budget. Optimistic start: every rung begins as if it had already succeeded
    4 times, so one failure (4/5 = 80%) keeps it and a second in a row moves this kind up a rung.
  - A failure moves this kind of step one rung up for the rest of the task (escalate on failure).
  - After 5 straight successes on a rung, try the rung below once.
  - Private-zone content never goes above rung 2: no rung for it, then ask the user or fail. Privacy beats cost.

Outcomes (kind, rung, ok, cost, seconds) are stored in memory.ladder_stats: no content, no task ids.
`lantern --router` prints the table.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import models

THRESHOLD = 0.8
DEESCALATE_AFTER = 5
WINDOW = 20          # recent outcomes per (kind, rung) that count
PRIOR = 4            # optimistic start: imaginary successes every rung begins with
PRIVATE_MAX = 2


@dataclass(frozen=True)
class Rung:
    n: int
    name: str
    model: str | None = None
    effort: str | None = None   # Responses API reasoning effort (dev.meta.ai/docs/reasoning)

    @property
    def cloud(self) -> bool:
        return self.n >= 3

    @property
    def badge(self) -> str:
        label = {0: "Routine", 1: "Rule", 2: models.badge(models.GLIMMER_LOCAL)}.get(self.n) or models.badge(self.model)
        return f"{label} · {self.effort}" if self.effort else label


RUNGS = (
    Rung(0, "routine"),
    Rung(1, "rules"),
    Rung(2, "local", models.GLIMMER_LOCAL),
    Rung(3, "spark-low", models.SPARK, "low"),
    Rung(4, "spark-high", models.SPARK, "high"),
    Rung(5, "wonder", models.WONDER),
)

# Which rungs can do a kind of step at all. The agent's own turns need tools on the Responses API (cloud rungs);
# computer use runs on Spark (native tool) or Wonder (function harness), models.CUA_PROTOCOL.
CAN: dict[str, tuple[int, ...]] = {
    "routine": (0,),
    "file_op": (1,),
    "lookup": (1,),
    "classify": (1, 2, 3),
    "extract": (2, 3, 4),
    "plan": (3, 4, 5),
    "reply": (3, 4, 5),
    "form_fill": (3, 4),
    "gui_action": (3, 5),
    "generate": (3, 4),
}
TOOL_KINDS = {
    "list_folders": "lookup", "list_dir": "lookup", "search_files": "lookup", "read_file": "lookup",
    "find_project": "lookup", "project_info": "lookup", "which_projects_use": "lookup", "list_reminders": "lookup",
    "write_file": "file_op", "move_file": "file_op", "organize_files": "file_op", "undo_moves": "file_op",
    "remember": "file_op", "forget": "file_op", "set_reminder": "file_op", "cancel_reminder": "file_op",
    "run_in_project": "file_op", "send_email": "file_op", "ask_private": "extract", "fill_form": "form_fill",
    "make_widget": "generate",
}


class LadderError(RuntimeError):
    """No rung may do this step (e.g. private content that needs more than the local model)."""


def tool_kind(name: str) -> str:
    return TOOL_KINDS.get(name, "lookup" if name.startswith("list") else "plan")


class Ladder:
    def __init__(self, memory, threshold: float = THRESHOLD, deescalate_after: int = DEESCALATE_AFTER,
                 window: int = WINDOW):
        self.memory = memory
        self.threshold, self.deescalate_after, self.window = threshold, deescalate_after, window

    def allowed(self, kind: str, private: bool = False) -> list[int]:
        rungs = [r for r in CAN.get(kind, CAN["plan"]) if not private or r <= PRIVATE_MAX]
        return rungs

    def _recent(self, kind: str) -> list[dict]:
        return self.memory.ladder_outcomes(kind, limit=500)

    def rate(self, kind: str, rung: int) -> tuple[int, float]:
        """(how many recent outcomes, success rate) for this kind at this rung."""
        rows = [r for r in self._recent(kind) if r["rung"] == rung][: self.window]
        return len(rows), (sum(r["ok"] for r in rows) + PRIOR) / (len(rows) + PRIOR)

    def streak(self, kind: str, rung: int) -> int:
        """Straight successes at this rung, most recent first."""
        n = 0
        for r in self._recent(kind):
            if r["rung"] != rung:
                continue
            if not r["ok"]:
                break
            n += 1
        return n

    def avg_cost(self, kind: str, rung: int) -> float:
        rows = [r for r in self._recent(kind) if r["rung"] == rung][: self.window]
        return sum(r["cost_usd"] for r in rows) / len(rows) if rows else 0.0

    def pick(self, kind: str, *, private: bool = False, floor: int | None = None,
             budget_left: float | None = None) -> Rung:
        allowed = self.allowed(kind, private)
        if not allowed:
            raise LadderError(f"no rung may do a '{kind}' step on private content; ask the user instead")
        choice = next((r for r in allowed if self.rate(kind, r)[1] >= self.threshold), allowed[-1])
        below = [r for r in allowed if r < choice]
        if below and self.streak(kind, choice) >= self.deescalate_after:
            choice = below[-1]   # 5 straight successes: see whether the cheaper rung copes now
        if floor is not None:
            above = [r for r in allowed if r >= floor]
            choice = max(choice, above[0] if above else allowed[-1])
        if budget_left is not None:   # an expensive rung that the task can't afford: the cheapest that fits
            while self.avg_cost(kind, choice) > budget_left and [r for r in allowed if r < choice]:
                choice = [r for r in allowed if r < choice][-1]
        return RUNGS[choice]

    def up(self, kind: str, rung: int, *, private: bool = False) -> Rung | None:
        """The next rung up for this kind, or None at the top (private content stops at rung 2)."""
        higher = [r for r in self.allowed(kind, private) if r > rung]
        return RUNGS[higher[0]] if higher else None

    def record(self, kind: str, rung: int, ok: bool, cost_usd: float = 0.0, seconds: float = 0.0) -> None:
        self.memory.log_ladder(kind, rung, ok, cost_usd, seconds)

    def table(self) -> list[dict]:
        """kind x rung -> outcomes, success %, average cost and time (all recorded outcomes)."""
        cells: dict[tuple[str, int], list[dict]] = {}
        for r in self.memory.ladder_outcomes(limit=100_000):
            cells.setdefault((r["kind"], r["rung"]), []).append(r)
        out = []
        for (kind, rung), rows in sorted(cells.items()):
            out.append({"kind": kind, "rung": rung, "badge": RUNGS[rung].badge, "n": len(rows),
                        "success": round(100 * sum(r["ok"] for r in rows) / len(rows)),
                        "avg_cost_usd": round(sum(r["cost_usd"] for r in rows) / len(rows), 6),
                        "avg_seconds": round(sum(r["seconds"] for r in rows) / len(rows), 2)})
        return out


class TaskLadder:
    """One task's view of the ladder: escalations made in this task (per kind) and whether it is private."""

    def __init__(self, ladder: Ladder, budget_left=lambda: None):
        self.ladder = ladder
        self.floor: dict[str, int] = {}
        self.budget_left = budget_left

    def pick(self, kind: str, private: bool = False) -> Rung:
        return self.ladder.pick(kind, private=private, floor=self.floor.get(kind), budget_left=self.budget_left())

    def failed(self, kind: str, rung: int, private: bool = False) -> Rung | None:
        """Escalate on failure: this kind of step goes one rung up for the rest of the task."""
        nxt = self.ladder.up(kind, max(rung, self.floor.get(kind, rung)), private=private)
        if nxt is not None:
            self.floor[kind] = nxt.n
        return nxt


def format_table(rows: list[dict]) -> str:
    if not rows:
        return "No ladder outcomes recorded yet: run some tasks with `lantern --serve` (the scheduler records them)."
    lines = [f"{'kind':<11} {'rung':<24} {'n':>4} {'success':>8} {'avg cost':>10} {'avg time':>9}"]
    for r in rows:
        lines.append(f"{r['kind']:<11} {str(r['rung']) + ' ' + r['badge']:<24} {r['n']:>4} {r['success']:>7}% "
                     f"{'$' + format(r['avg_cost_usd'], '.4f'):>10} {format(r['avg_seconds'], '.1f') + ' s':>9}")
    return "\n".join(lines)
