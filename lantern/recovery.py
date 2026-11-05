"""Resilience engine (S2): when a step fails, say what kind of failure it was and try a different way.

  class            detected by                                         strategy
  transient        timeouts, 429, 5xx, connection errors               wait with backoff, parked (S1); others run
  tool_error       {"error": ...} from a tool                          feed it back; the same failing call twice in a
                                                                       row is refused, so the model must change course
  approach_failed  verified: false (a postcondition didn't hold)       the next approach for that step (APPROACHES),
                                                                       and the ladder (S3) moves that kind of step up
  model_confused   bad JSON arguments twice, the same call 3 times,    escalate one ladder rung (S3)
                   no progress for 6 turns
  needs_human      needs_user / fields left for the user / CAPTCHA     ask_user, which parks the task (S1)
  impossible       a step failed too often, every rung tried,          stop with a report of everything tried
                   or a spending limit hit

Every attempt is a step of kind "attempt" (attempt number, class, strategy, rung, cost so far, result), so the
receipt tells the whole recovery story.

Hard rules, in code: a send, submit or move (NON_IDEMPOTENT) that ran, or may have run, is never run again in
the same task unless a check proves the first one didn't happen. A step gets MAX_ATTEMPTS failed tries.
Spending limits stay with budget.py (checked before every model call).
"""
from __future__ import annotations

import hashlib
import json
import random
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

IDEMPOTENT = frozenset({
    "list_folders", "list_dir", "search_files", "read_file", "ask_private", "find_project", "project_info",
    "which_projects_use", "list_reminders", "remember", "forget", "cancel_reminder", "write_file", "finish",
    "run_in_project", "make_widget",
})
NON_IDEMPOTENT = frozenset({"send_email", "fill_form", "move_file", "organize_files", "set_reminder", "undo_moves"})

CLASSES = ("transient", "tool_error", "approach_failed", "model_confused", "needs_human", "impossible")

# What to try next when a step ran but its check failed, in order. The model gets the next one as a hint;
# for form filling the ladder (S3) also moves the computer-use model up a rung.
APPROACHES = {
    "fill_form": ["fill it again: Lantern will use a stronger model for the parts it couldn't do",
                  "ask the user to finish the form themselves (ask_user)"],
    "write_file": ["read the file back to see what is there, then write it once more",
                   "ask the user (ask_user)"],
    "move_file": ["list the folder to see where the file is now, then move it", "ask the user (ask_user)"],
    "organize_files": ["list the folders, then send a smaller plan with only the moves that failed",
                       "ask the user (ask_user)"],
    "send_email": ["do not send again: the first one may have gone; say so in your summary",
                   "ask the user whether it arrived (ask_user)"],
    "set_reminder": ["set it with an exact ISO time like 2026-10-27T18:00", "ask the user (ask_user)"],
    "run_in_project": ["read the error output, fix the cause, then run it again", "ask the user (ask_user)"],
}
DEFAULT_APPROACHES = ["try a different tool or different arguments", "ask the user (ask_user)"]


def is_idempotent(name: str) -> bool:
    return name in IDEMPOTENT


def uncertain_result(name: str) -> dict:
    """What the model gets for a non-idempotent call that was running when Lantern stopped."""
    return {"error": f"Lantern stopped while {name} was running, so it may or may not have happened. "
                     "Check first (list the folder, look in the Sent mail, open the confirmation) and only "
                     "then decide whether to do it again.",
            "uncertain": True}


def signature(name: str, args: dict) -> str:
    return name + ":" + hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()[:16]


def effect_key(name: str, args: dict) -> str:
    """What a non-idempotent call changes in the world. Two calls with the same effect key would do the same
    thing twice (re-wording a form goal or an email body doesn't make a second submit or send safe)."""
    if name == "fill_form":
        target = str(args.get("url", ""))
    elif name == "send_email":
        target = json.dumps([sorted(map(str, args.get("to") or [])), str(args.get("subject", "")).strip().lower()])
    elif name == "move_file":
        target = str(args.get("source", ""))
    elif name == "organize_files":
        target = json.dumps(sorted(str(m.get("source", "")) for m in args.get("moves") or [] if isinstance(m, dict)))
    elif name == "set_reminder":
        target = json.dumps([str(args.get("text", "")).strip().lower(), str(args.get("when", ""))])
    else:
        return signature(name, args)
    return f"{name}:{target}"


def step_key(name: str, args: dict) -> str:
    """Which step a call works on: the tool plus its target (path, url, recipients), not every argument."""
    target = next((args[k] for k in ("url", "path", "source", "to", "name", "key") if k in args), "")
    return f"{name}:{json.dumps(target, sort_keys=True, default=str)[:200]}"


class Impossible(Exception):
    """Recovery gave up on this task; the message is the report of everything tried."""


@dataclass
class Limits:
    max_attempts: int = 3          # failed tries at one step before the task stops
    transient_rounds: int = 2      # extra waits after the client's own retries ran out
    transient_wait: float = 20.0   # seconds, doubled each round, with jitter
    loop_repeats: int = 3          # the same call this many times = stuck in a loop
    bad_args: int = 2              # unreadable arguments this many times = confused
    no_progress_turns: int = 6


@dataclass
class Attempt:
    n: int
    cls: str
    strategy: str
    tool: str = ""
    rung: int | None = None
    cost_usd: float = 0.0
    result: str = ""
    detail: dict = field(default_factory=dict)

    def public(self) -> dict:
        return {"attempt": self.n, "class": self.cls, "strategy": self.strategy, "tool": self.tool, "rung": self.rung,
                "cost_usd": round(self.cost_usd, 6), "result": self.result[:200], **self.detail}


def _happened(name: str, result: Any) -> bool:
    """Did this non-idempotent call (maybe) act? Only clear evidence of 'nothing happened' says no."""
    if not isinstance(result, dict):
        return True
    if result.get("uncertain"):
        return True
    if "error" in result or result.get("reason") == "user declined" or result.get("refused_by_recovery"):
        return False
    if name == "fill_form":
        return bool(result.get("submitted"))
    if name == "send_email":
        return result.get("sent", True) is not False
    if name in ("organize_files", "undo_moves"):
        return bool(result.get("moved")) or result.get("verified") is True
    if name == "move_file":
        return result.get("done", True) is not False or result.get("verified") is True
    return True


class Recovery:
    """One task's recovery state. Plugged into taskrun.ResumableAgent (before_call / after_call / ...)."""

    def __init__(self, memory, task_id: int, *, emit: Callable[[dict], None] | None = None,
                 rung: Callable[[], int | None] = lambda: None,
                 escalate: Callable[[str], int | None] | None = None,
                 policy=None, limits: Limits | None = None, sleep: Callable[[float], None] | None = None):
        self.memory, self.task_id = memory, task_id
        self.emit = emit or (lambda e: None)
        self.rung = rung
        self.escalate_to = escalate      # (reason) -> the new rung, or None at the top of the ladder
        self.policy = policy             # for checking whether a move happened
        self.limits = limits or Limits()
        self.sleep = sleep
        self.attempts: list[Attempt] = []
        self.sigs: list[str] = []                     # every call, in order
        self.acted: dict[str, Any] = {}               # non-idempotent effect key -> its result
        self.failures: Counter = Counter()            # step key -> failed tries
        self.last: tuple[str, bool] | None = None     # (signature, failed) of the previous call
        self.bad_args = 0
        self.quiet_turns = 0
        self.progress_sigs: set[str] = set()
        self.give_up: str | None = None               # set when the task must stop: the report
        self._restore()

    def _restore(self) -> None:
        """Rebuild the guard from the task's own steps, so parking or a restart never resets it
        (a send that happened before the pause stays 'done' after it)."""
        pending: dict[str, dict] = {}
        for step in self.memory.steps(self.task_id):
            try:
                data = json.loads(step["content"])
            except (TypeError, ValueError):
                continue
            if step["kind"] == "tool_call" and isinstance(data, dict):
                pending[str(data.get("name"))] = data.get("args") or {}
            elif step["kind"] == "tool_result" and isinstance(data, dict):
                name, result = str(data.get("name")), data.get("result")
                args = pending.pop(name, None)
                if args is None:
                    continue
                sig = signature(name, args)
                self.sigs.append(sig)
                if name not in IDEMPOTENT:
                    self._acted(name, args, result)
                if isinstance(result, dict) and self.classify(name, result) in ("tool_error", "approach_failed"):
                    self.failures[step_key(name, args)] += 1
            elif step["kind"] == "attempt" and isinstance(data, dict):
                self.attempts.append(Attempt(int(data.get("attempt", len(self.attempts) + 1)), str(data.get("class")),
                                             str(data.get("strategy")), str(data.get("tool") or ""), data.get("rung"),
                                             float(data.get("cost_usd") or 0), str(data.get("result") or "")))

    # ---- the attempt log -----------------------------------------------------------------
    def _log(self, cls: str, strategy: str, tool: str = "", result: str = "", **detail) -> Attempt:
        cost = self.memory.task_usage(self.task_id)["cost_usd"]
        a = Attempt(len(self.attempts) + 1, cls, strategy, tool, self.rung(), cost, result, detail)
        self.attempts.append(a)
        self.memory.log_step(self.task_id, "attempt", a.public())
        self.emit({"type": "recovery", "task_id": self.task_id, **a.public()})
        return a

    def report(self, why: str) -> str:
        """Everything tried, in plain words: the summary of a task recovery gave up on."""
        tried = [f"{a.n}) {a.tool or 'model'}: {a.cls.replace('_', ' ')} -> {a.strategy.replace('_', ' ')}"
                 + (f" ({a.result[:80]})" if a.result else "") for a in self.attempts]
        return f"Stopped: {why}. Tried: " + ("; ".join(tried[-8:]) if tried else "nothing else") + "."

    # ---- before a tool runs --------------------------------------------------------------
    def before_call(self, task_id: int, name: str, args: dict) -> dict | None:
        """A refusal result, or None to let the call run."""
        sig = signature(name, args)
        key = step_key(name, args)
        if self.failures[key] >= self.limits.max_attempts:
            self.give_up = self.report(f"{name} failed {self.failures[key]} times on the same target")
            return self._refuse(name, "impossible", "stop", "this step already failed too often; Lantern stops here")
        effect = effect_key(name, args)
        if name not in IDEMPOTENT and effect in self.acted and _happened(name, self.acted[effect]):
            if not self._proven_not_happened(name, args):
                return self._refuse(name, "needs_human", "refuse_duplicate",
                                    f"this exact {name} already ran in this task (or may have), and Lantern never "
                                    "repeats a send, submit or move. Check the result, or ask the user (ask_user).")
        if self.last == (sig, True):
            return self._refuse(name, "tool_error", "different_approach",
                                "the same call just failed; choose a different approach or different arguments")
        if self.sigs.count(sig) + 1 >= self.limits.loop_repeats:
            self.sigs.append(sig)
            refusal = self._refuse(name, "model_confused", "refuse_loop",
                                   f"this would be the {self.limits.loop_repeats}rd identical call; you seem stuck. "
                                   "Do something different or ask the user.")
            self.confused("the same call three times")
            return refusal
        self.sigs.append(sig)
        return None

    def _acted(self, name: str, args: dict, result: Any) -> None:
        """Remember what a non-idempotent call did. Once it (maybe) happened, it stays that way."""
        effect = effect_key(name, args)
        if effect not in self.acted or not _happened(name, self.acted[effect]):
            self.acted[effect] = result

    def _refuse(self, name: str, cls: str, strategy: str, why: str) -> dict:
        self._log(cls, strategy, name, why)
        self.last = None
        return {"error": f"refused: {why}", "refused_by_recovery": True,
                "recovery": {"class": cls, "strategy": strategy}}

    def _proven_not_happened(self, name: str, args: dict) -> bool:
        """A check on disk that the earlier move didn't happen (its files are all still where they were)."""
        if self.policy is None or name not in ("move_file", "organize_files"):
            return False
        sources = [args.get("source")] if name == "move_file" else [m.get("source") for m in args.get("moves", [])]
        try:
            return all(s and Path(self.policy.resolve(s)).exists() for s in sources)
        except Exception:
            return False

    # ---- after a tool ran ----------------------------------------------------------------
    def after_call(self, task_id: int, name: str, args: dict, result: Any) -> Any:
        sig, key = signature(name, args), step_key(name, args)
        if name not in IDEMPOTENT:
            self._acted(name, args, result)
        if not isinstance(result, dict) or result.get("refused_by_recovery"):
            return result
        cls = self.classify(name, result)
        self.last = (sig, cls is not None)
        if cls is None:
            if sig not in self.progress_sigs:
                self.progress_sigs.add(sig)
                self.quiet_turns = -1   # end_turn adds one back: this turn made progress
            return result
        if cls in ("tool_error", "approach_failed"):
            self.failures[key] += 1
        tries = self.failures[key]
        if cls == "needs_human":
            why = str(result.get("reason") or "it needs something only the user can do")
            hint = (f"Call ask_user: tell the user exactly what they must do ({why[:160]}) and wait for their answer. "
                    "Never ask for passwords, codes, card or ID numbers in chat.")
            strategy = "ask_user"
        elif cls == "tool_error":
            hint, strategy = "Read the error and choose a different approach; the same call won't run twice.", "feed_back"
        else:
            options = APPROACHES.get(name, DEFAULT_APPROACHES)
            hint, strategy = options[min(tries - 1, len(options) - 1)], "next_approach"
        out = dict(result)
        out["recovery"] = {"class": cls, "strategy": strategy, "attempt": tries, "next": hint}
        self._log(cls, strategy, name, str(result.get("error") or result.get("reason") or "not verified"),
                  next=hint, tries=tries)
        if tries >= self.limits.max_attempts and cls != "needs_human":
            out["recovery"]["last_try"] = True
            self.give_up = self.report(f"{name} failed {tries} times")
        return out

    @staticmethod
    def classify(name: str, result: dict) -> str | None:
        """None when the step went fine."""
        if result.get("needs_user") or (result.get("left_for_you") and result.get("done") is False):
            return "needs_human"
        if "error" in result:
            if result.get("uncertain"):
                return "needs_human"
            return "tool_error"
        if result.get("advanced") and result.get("submitted"):
            return None   # a multi-page form's middle page: accepted, moved on (no confirmation of its own)
        if result.get("verified") is False or result.get("done") is False:
            if result.get("reason") == "user declined":
                return None   # the user said no: not a failure to work around (agent rules: no workarounds)
            return "approach_failed"
        return None

    # ---- the model itself ------------------------------------------------------------------
    def bad_arguments(self, name: str, error: str) -> None:
        self.bad_args += 1
        self._log("tool_error", "feed_back", name, error)
        if self.bad_args >= self.limits.bad_args:
            self.bad_args = 0
            self.confused("unreadable tool arguments twice")

    def end_turn(self) -> None:
        """After every model turn's calls: no progress for a while means the model is stuck."""
        self.quiet_turns += 1
        if self.quiet_turns >= self.limits.no_progress_turns:
            self.quiet_turns = 0
            self.confused(f"no progress in {self.limits.no_progress_turns} steps")

    def confused(self, why: str) -> None:
        new = self.escalate_to(why) if self.escalate_to is not None else None
        if new is None and self.escalate_to is not None:
            self._log("impossible", "stop", "", f"{why}; every model rung was tried")
            self.give_up = self.report(f"{why}, and every model rung was tried")
            return
        self._log("model_confused", "escalate", "", why, to_rung=new)

    def model_error(self, exc: BaseException, round_: int) -> float | None:
        """A model call failed after the client's own retries. Seconds to wait before one more try, or None."""
        from .budget import BudgetError
        from .reliability import explain, is_transient
        if isinstance(exc, BudgetError):
            self._log("impossible", "stop", "", explain(exc))
            return None
        if not is_transient(exc) or round_ > self.limits.transient_rounds:
            return None
        wait = self.limits.transient_wait * 2 ** (round_ - 1) * random.uniform(0.8, 1.2)
        self._log("transient", "backoff", "", explain(exc), wait_s=round(wait, 1))
        return wait

    def failed_summary(self, why: str) -> str:
        """The task's failure summary, with the recovery story when there is one."""
        return self.report(why) if self.attempts else why
