"""Resumable task runs (S1): the agent loop with a checkpoint at every step, so a task can wait for the user
without holding a worker, and pick up where it was after Lantern restarts.

`Agent` (agent.py) runs a task from start to finish on one thread. `ResumableAgent` runs the same loop, and
  - saves a checkpoint (memory.task_state) when a model turn arrives and around every tool call: the response
    id, the turn's function calls, the results so far, which call is running right now, and the task's taint;
  - parks the task when the model calls `ask_user`: the question is saved, `Parked` is raised and the thread
    ends. `resume()` later finishes that same turn with the answer as the call's result and goes on with the
    same `previous_response_id` chain (or, if the API no longer has it, a fresh chain with a recap);
  - after a restart, `resume()` continues the saved turn: calls that finished are not run again, and a call
    that was running when Lantern stopped goes to recovery.py, which never repeats a send, submit or move
    blindly.

The checkpoint holds the task's own data (results, the question, the answer), so memory stores it encrypted.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable

from .agent import Agent, Result
from .tools import AskUser, ToolError

STATE_VERSION = 1


class Parked(BaseException):
    """The task is parked (waiting for the user, or Lantern is shutting down); its checkpoint is saved.

    A BaseException on purpose: it is control flow, not an error, so `except Exception` handlers on the way
    out (Agent.run marks a task failed there) let it pass untouched."""

    def __init__(self, reason: str, question: str = "", options: list[str] | None = None, tainted: bool = False):
        super().__init__(reason)
        self.reason, self.question, self.options, self.tainted = reason, question, list(options or []), tainted


class _Cancelled(Exception):
    """STOP arrived between calls."""


Checkpoint = Callable[[int, dict, str, str], None]   # (task_id, state, phase, reason)


def chain_lost(exc: BaseException) -> bool:
    """Does this error say the API no longer has the previous response? (Then we replay with a recap.)
    The API reference doesn't document this error, so this is a guess kept narrow: a 400/404 that names
    the previous response."""
    from .reliability import status_of
    text = str(getattr(exc, "body", "") or "") + " " + str(exc)
    return status_of(exc) in (400, 404) and "previous" in text.lower() and "response" in text.lower()


class ResumableAgent(Agent):
    def __init__(self, client, memory, toolbox, *, checkpoint: Checkpoint | None = None,
                 on_turn: Callable[[int], None] | None = None, recovery: Any = None, **kw):
        super().__init__(client, memory, toolbox, **kw)
        toolbox.can_park = True
        self.checkpoint = checkpoint or (lambda task_id, state, phase, reason: None)
        self.on_turn = on_turn or (lambda task_id: None)   # scheduler: share the worker, stop on shutdown
        self.recovery = recovery                           # recovery.Recovery (S2), optional

    def _create(self, task_id: int, **kw):
        """A model call; when it still fails after the client's retries, recovery may wait (parked) and try again."""
        round_ = 0
        while True:
            try:
                return super()._create(task_id, **kw)
            except Exception as e:
                round_ += 1
                wait = self.recovery.model_error(e, round_) if self.recovery is not None else None
                if wait is None:
                    raise
                (self.recovery.sleep or time.sleep)(wait)

    def _failed(self, task_id: int, e: Exception, why: str | None = None) -> None:
        from .reliability import explain
        if self.recovery is not None:
            why = self.recovery.failed_summary(why or explain(e))
        super()._failed(task_id, e, why)

    # ---- state ---------------------------------------------------------------------
    def _save(self, task_id: int, state: dict, phase: str = "running", reason: str = "") -> None:
        state["tainted"] = bool(getattr(self.toolbox, "tainted", False))
        state["untrusted_emails"] = sorted(getattr(self.toolbox, "untrusted_emails", set()) or set())
        self.checkpoint(task_id, state, phase, reason)

    def _restore_taint(self, state: dict) -> None:
        """A task that read untrusted content stays tainted after parking or a restart."""
        if state.get("tainted"):
            self.toolbox.tainted = True
        self.toolbox.untrusted_emails = set(getattr(self.toolbox, "untrusted_emails", set()) or set()) | set(
            state.get("untrusted_emails") or [])

    # ---- start ---------------------------------------------------------------------
    def _run(self, task_id: int, goal: str) -> Result:
        if hasattr(self.toolbox, "begin_task"):
            self.toolbox.begin_task(task_id, goal)
        if hasattr(self.client, "set_task"):
            self.client.set_task(task_id)
        self.memory.log_step(task_id, "plan", {"goal": goal})
        self.on_event("task", {"id": task_id, "goal": goal})
        instructions = self.instructions()
        state = {"v": STATE_VERSION, "goal": goal, "instructions": instructions, "step": 1, "response_id": None,
                 "calls": [], "results": {}, "in_flight": None, "ask": None}
        self._save(task_id, state)
        response = self._create(task_id, instructions=instructions, input=goal, tools=self.tools)
        return self._loop(task_id, state, response)

    # ---- resume (after an answer, or after a restart) -----------------------------------
    def resume(self, task_id: int, state: dict, answer: str | None = None) -> Result:
        task = self.memory.get_task(task_id)
        self.cancel.clear()
        try:
            return self._resume(task_id, task.goal, state, answer)
        except Exception as e:
            self._failed(task_id, e)
            raise

    def _resume(self, task_id: int, goal: str, state: dict, answer: str | None) -> Result:
        if hasattr(self.toolbox, "begin_task"):
            self.toolbox.begin_task(task_id, goal)
        self._restore_taint(state)
        if hasattr(self.client, "set_task"):
            self.client.set_task(task_id)
        state.setdefault("results", {})
        if not state.get("response_id"):   # stopped before the first model answer: nothing ran yet
            state.update(step=1, calls=[], results={}, in_flight=None, ask=None)
            self._save(task_id, state)
            response = self._create(task_id, instructions=state["instructions"], input=goal, tools=self.tools)
            return self._loop(task_id, state, response)
        ask = state.get("ask")
        if ask:
            if answer is None:   # still waiting for the user
                raise Parked("waiting_user", ask["question"], ask.get("options"), ask.get("tainted", False))
            result = {"answer": answer}
            state["results"][ask["call_id"]] = result
            state["ask"] = None
            self.memory.log_step(task_id, "tool_result", {"name": "ask_user", "result": result})
            self.on_event("tool", {"name": "ask_user", "args": {"question": ask["question"]}, "result": result,
                                   "badge": "you"})
            self._save(task_id, state)
        state["chain_check"] = True   # the next call may find the API dropped the old chain
        return self._loop(task_id, state, None)

    # ---- the loop --------------------------------------------------------------------
    def _loop(self, task_id: int, state: dict, response: Any) -> Result:
        calls_made: list = []
        while state["step"] <= self.max_steps:
            step = state["step"]
            if self.cancel.is_set():
                return self._stop(task_id, "cancelled", "stopped by the user", step, calls_made)
            if response is not None:   # a new turn from the model (None: continue the saved turn)
                fcalls = [o for o in response.output if getattr(o, "type", None) == "function_call"]
                if not fcalls:
                    text = getattr(response, "output_text", "") or ""
                    summary = text.strip().splitlines()[0][:200] if text.strip() else "(no reply)"
                    self.memory.log_step(task_id, "reply", text)
                    self.memory.finish_task(task_id, "done", summary)
                    return self._result(task_id, "done", summary, text, step, calls_made)
                state.update(response_id=response.id, results={}, in_flight=None, ask=None,
                             calls=[{"name": fc.name, "call_id": fc.call_id, "arguments": fc.arguments or "{}"}
                                    for fc in fcalls])
                self._save(task_id, state)
            try:
                outputs, finished = self._calls(task_id, state, calls_made)
            except _Cancelled:
                return self._stop(task_id, "cancelled", "stopped by the user", step, calls_made)
            if self.recovery is not None and not finished:
                self.recovery.end_turn()
                if self.recovery.give_up:   # impossible: stop with the report of everything tried
                    return self._stop(task_id, "failed", self.recovery.give_up, step, calls_made)
            if finished:
                status, summary = finished
                response = self._turn(task_id, state, outputs)   # the in-character closing line
                reply = getattr(response, "output_text", "") or summary
                self.memory.log_step(task_id, "reply", reply)
                self.memory.finish_task(task_id, status, summary)
                return self._result(task_id, status, summary, reply, step, calls_made)
            state["step"] = step + 1
            self._save(task_id, state)
            self.on_turn(task_id)
            response = self._turn(task_id, state, outputs)
        why = f"stopped after {self.max_steps} steps without finishing"
        return self._stop(task_id, "failed", self.recovery.failed_summary(why) if self.recovery else why,
                          self.max_steps, calls_made)

    def _turn(self, task_id: int, state: dict, outputs: list[dict]) -> Any:
        """The next model call on the saved chain; a recap starts a fresh chain if the API lost the old one."""
        kw = dict(instructions=state["instructions"], tools=self.tools)
        if not state.pop("chain_check", False):
            return self._create(task_id, previous_response_id=state["response_id"], input=outputs, **kw)
        try:
            return self._create(task_id, previous_response_id=state["response_id"], input=outputs, **kw)
        except Exception as e:
            if not chain_lost(e):
                raise
            self.memory.log_step(task_id, "note", "the model API no longer had this task's earlier turns; "
                                                  "continued with a recap")
            return self._create(task_id, input=self._recap(task_id, state, outputs), **kw)

    def _recap(self, task_id: int, state: dict, outputs: list[dict]) -> str:
        names = {c["call_id"]: c["name"] for c in state.get("calls", [])}
        latest = "; ".join(f"{names.get(o['call_id'], '?')} -> {o['output'][:600]}" for o in outputs)
        return (f"{state['goal']}\n\n[Continuing task #{task_id} after a pause; the earlier turns are no longer "
                f"available. Results of the last step: {latest or 'none'}. Check before repeating anything that "
                "changes something.]")

    def _calls(self, task_id: int, state: dict, calls_made: list) -> tuple[list[dict], tuple | None]:
        """Run the turn's function calls that haven't run yet. Returns the outputs and (status, summary) if
        `finish` was called. Raises Parked when the model asked the user something."""
        finished = None
        asked: tuple[str, dict, AskUser] | None = None
        for c in state["calls"]:
            cid, name = c["call_id"], c["name"]
            try:
                args = json.loads(c["arguments"] or "{}")
                if not isinstance(args, dict):
                    raise ValueError("arguments must be an object")
            except (json.JSONDecodeError, ValueError) as e:
                args, parse_error = {}, {"error": f"could not parse arguments: {e}"}
            else:
                parse_error = None
            if name == "finish" and parse_error is None:
                status = args.get("status") if args.get("status") in ("done", "failed") else "failed"
                finished = (status, str(args.get("summary", "")).strip() or "(no summary)")
            if cid in state["results"]:
                continue   # ran before this task was parked or Lantern restarted
            if self.cancel.is_set():
                raise _Cancelled()
            if parse_error is not None:
                result = parse_error
                if self.recovery is not None:
                    self.recovery.bad_arguments(name, parse_error["error"])
            elif name == "finish":
                result = {"ok": True}
            elif cid == state.get("in_flight"):
                result = self._interrupted(task_id, state, name, args)
            elif name == "ask_user" and asked is not None:
                result = {"error": "ask one question at a time; this one waits for the first answer"}
            else:
                result = self._tool(task_id, state, cid, name, args)
                if isinstance(result, AskUser):
                    asked = (cid, args, result)
                    continue
            self._record(task_id, state, cid, name, args, result, calls_made)
        if asked is not None:
            cid, args, q = asked
            state["ask"] = {"call_id": cid, "question": q.question, "options": q.options, "tainted": q.tainted}
            state["in_flight"] = None
            self.memory.log_step(task_id, "tool_call", {"name": "ask_user", "args": args})
            self._save(task_id, state, "parked", "waiting_user")
            raise Parked("waiting_user", q.question, q.options, q.tainted)
        outputs = [{"type": "function_call_output", "call_id": c["call_id"],
                    "output": json.dumps(state["results"][c["call_id"]], ensure_ascii=False, default=str)}
                   for c in state["calls"]]
        return outputs, finished

    def _tool(self, task_id: int, state: dict, cid: str, name: str, args: dict) -> Any:
        state["in_flight"] = cid
        self._save(task_id, state)
        if self.recovery is not None:
            refused = self.recovery.before_call(task_id, name, args)
            if refused is not None:
                return refused
        try:
            result = self.toolbox.call(name, args)
        except ToolError as e:
            result = {"error": str(e)}
        except AskUser as q:
            return q
        if self.recovery is not None:
            result = self.recovery.after_call(task_id, name, args, result)
        return result

    def _interrupted(self, task_id: int, state: dict, name: str, args: dict) -> Any:
        """This call was running when Lantern stopped. Run it again only when that can't do anything twice."""
        if state.get("approval_open"):   # it was still waiting for a yes: nothing happened, ask again
            state["approval_open"] = False
            return self._tool(task_id, state, state["in_flight"], name, args)
        from . import recovery
        if recovery.is_idempotent(name):
            return self._tool(task_id, state, state["in_flight"], name, args)
        result = recovery.uncertain_result(name)
        if self.recovery is not None:   # remembered as "may have happened": never repeated blindly
            result = self.recovery.after_call(task_id, name, args, result)
        return result

    def _record(self, task_id: int, state: dict, cid: str, name: str, args: dict, result: Any,
                calls_made: list) -> None:
        state["results"][cid] = result
        state["in_flight"] = None
        self._save(task_id, state)
        if name == "ask_private" and isinstance(result, dict) and "answer" in result:
            self.memory.mark_private(task_id)
        calls_made.append({"name": name, "args": args, "result": result})
        self.memory.log_step(task_id, "tool_call", {"name": name, "args": args})
        self.memory.log_step(task_id, "tool_result", {"name": name, "result": result})
        self.on_event("tool", {"name": name, "args": args, "result": result, "badge": self.tool_badge(name, result)})
