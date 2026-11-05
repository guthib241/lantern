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

With a ladder (ladder.py, S3), every model turn runs on the rung picked for its kind of step: Spark at low
or high reasoning effort, or Wonder. A switch to another model starts a fresh chain with a recap, because
chaining responses across models isn't documented. Each turn's and each tool's outcome is recorded.

The checkpoint holds the task's own data (results, the question, the answer), so memory stores it encrypted.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable

from . import models
from .agent import NOT_DONE, Agent, Result, track_unconfirmed
from .labels import HANDLE
from .tools import AskUser, ToolError

STATE_VERSION = 1
PRIVATE_SHOWN = "‹private: shown on your computer›"   # what a channel gets instead of a private value (S0)


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


# What a recap may repeat of a step's result: Lantern's own small fields, never content (that stays a handle).
BRIEF_KEYS = ("path", "count", "moved", "of", "sent", "to", "due", "url", "reason", "error", "answer_type", "found",
              "doc", "submitted", "form_url", "already_filed")


def chain_lost(exc: BaseException) -> bool:
    """Does this error say the API no longer has the previous response? (Then we replay with a recap.)
    A 400/404 that names the previous response. The live API's wording (seen Oct 27-28, mid-task, seconds after
    the response was made): "referenced response not found or expired"."""
    from .reliability import status_of
    text = (str(getattr(exc, "body", "") or "") + " " + str(exc)).lower()
    return status_of(exc) in (400, 404) and "response" in text and (
        "previous" in text or "referenced response" in text or "not found or expired" in text)


class ResumableAgent(Agent):
    def __init__(self, client, memory, toolbox, *, checkpoint: Checkpoint | None = None,
                 on_turn: Callable[[int], None] | None = None, recovery: Any = None, ladder: Any = None, **kw):
        super().__init__(client, memory, toolbox, **kw)
        toolbox.can_park = True
        self.checkpoint = checkpoint or (lambda task_id, state, phase, reason: None)
        self.on_turn = on_turn or (lambda task_id: None)   # scheduler: share the worker, stop on shutdown
        self.recovery = recovery                           # recovery.Recovery (S2), optional
        self.ladder = ladder                               # ladder.TaskLadder (S3), optional
        self.rung_now: Any = None                          # the rung of the latest model call
        self.kind_now = "plan"
        self._turn_meta: tuple | None = None               # (kind, rung, seconds, cost) of the latest call
        self._tool_seconds = 0.0
        self._public: tuple | None = None                  # S0: (summary, reply) without private values
        inner = self.on_event

        def on_event(kind: str, data: dict) -> None:       # usage events carry the rung for the model badge
            if kind == "usage" and self.rung_now is not None:
                data = {**data, "rung": self.rung_now.n, "rung_badge": self.rung_now.badge, "kind": self.kind_now}
            if kind == "done" and self._public is not None:   # channels get the copy without private values
                data = {**data, "summary_public": self._public[0], "reply_public": self._public[1]}
            inner(kind, data)
        self.on_event = on_event

    def instructions(self) -> str:
        """With the S0 toolbox, the planner is also told how handles work (a fixed block: the prefix stays cacheable)."""
        from .agent import BASE_RULES
        from .security import S0_RULES
        base = super().instructions()
        return base.replace(BASE_RULES, BASE_RULES + S0_RULES, 1) if hasattr(self.toolbox, "handles") else base

    def _finish(self, task_id: int, status: str, summary: str, reply: str, step: int, calls_made: list) -> Result:
        """Handles in the reply are filled in for the user's own screen only (S0). The stored summary is the filled
        one, but a task whose summary quotes private or untrusted content is kept out of later briefings."""
        handles = getattr(self.toolbox, "handles", None)
        self._public = None
        if handles is not None:
            shown_reply, lab_r, used_r = handles.expand(reply)
            shown_summary, lab_s, used_s = handles.expand(summary)
            if used_r or used_s:
                label = lab_r.join(lab_s)
                self._public = (handles.expand(summary, private_as=PRIVATE_SHOWN)[0],
                                handles.expand(reply, private_as=PRIVATE_SHOWN)[0])
                if label.confidential:
                    self.memory.mark_private(task_id)
                elif label.untrusted:
                    self.memory.mark_quoted(task_id)
            # a handle the model made up (or one from a step that failed) is never shown raw: live, Oct 28 a reply
            # read "$h1 is sitting in your Private folder" after ask_private had refused
            reply, summary = (HANDLE.sub("(that)", shown_reply), HANDLE.sub("(that)", shown_summary))
            if self._public is not None:
                self._public = tuple(HANDLE.sub("(that)", x) for x in self._public)
        self.memory.log_step(task_id, "reply", reply)
        self.memory.finish_task(task_id, status, summary)
        try:
            return self._result(task_id, status, summary, reply, step, calls_made)
        finally:
            self._public = None

    def _model(self, task_id: int, kind: str, rung: Any = None, **kw):
        """One model call on the ladder's rung for this kind of step (or the agent's model without a ladder)."""
        if rung is None and self.ladder is not None:
            rung = self.ladder.pick(kind)
        if rung is not None and rung.model:
            self.model = models.check_model(rung.model)
            if rung.effort:
                kw["reasoning"] = {"effort": rung.effort}
        self.rung_now, self.kind_now = rung, kind
        t0, cost0 = time.monotonic(), self.memory.task_usage(task_id)["cost_usd"]
        response = self._create(task_id, **kw)
        self._turn_meta = (kind, rung, time.monotonic() - t0, self.memory.task_usage(task_id)["cost_usd"] - cost0)
        return response

    def escalate(self, why: str) -> int | None:
        """Recovery found the model confused: the agent's turns go one rung up. None at the top."""
        if self.ladder is None:
            return None
        nxt = self.ladder.failed("plan", self.rung_now.n if self.rung_now is not None else 3)
        return nxt.n if nxt is not None else None

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
        if self.ladder is not None:
            state["floors"] = dict(self.ladder.floor)   # escalations made in this task survive a pause
        if hasattr(self.toolbox, "dump_state"):        # S0 handles and labels survive a pause too (encrypted)
            state["toolbox"] = self.toolbox.dump_state()
        self.checkpoint(task_id, state, phase, reason)

    def _restore_taint(self, state: dict) -> None:
        """A task that read untrusted content stays tainted after parking or a restart."""
        if state.get("tainted"):
            self.toolbox.tainted = True
        self.toolbox.untrusted_emails = set(getattr(self.toolbox, "untrusted_emails", set()) or set()) | set(
            state.get("untrusted_emails") or [])

    # ---- start ---------------------------------------------------------------------
    def _run(self, task_id: int, goal: str, instructions_suffix: str = "",
             allowed_tools: set[str] | None = None, taint: bool = False) -> Result:
        """A task from its goal. An agent pack's run (T3a) adds its instructions, allows only its tools and starts
        tainted; the allowlist is saved with the task, so a pack task parked for an approval keeps it."""
        if hasattr(self.toolbox, "begin_task"):
            self.toolbox.begin_task(task_id, goal)
        if allowed_tools is not None:
            self.toolbox.allowed_tools = set(allowed_tools)
        if taint:   # a shared pack was written by someone else: untrusted from the start
            self.toolbox.tainted = True
        if hasattr(self.client, "set_task"):
            self.client.set_task(task_id)
        self.memory.log_step(task_id, "plan", {"goal": goal})
        self.on_event("task", {"id": task_id, "goal": goal})
        instructions = self.instructions()
        if instructions_suffix:
            instructions += "\n\n" + instructions_suffix
        state = {"v": STATE_VERSION, "goal": goal, "instructions": instructions, "step": 1, "response_id": None,
                 "calls": [], "results": {}, "in_flight": None, "ask": None}
        if allowed_tools is not None:
            state["allowed_tools"] = sorted(allowed_tools)
        self._save(task_id, state)
        response = self._model(task_id, "plan", instructions=instructions, input=goal, tools=self.tools)
        state["model"] = self.model
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
        if state.get("allowed_tools") is not None:   # an agent pack's task keeps its allowlist after a pause
            self.toolbox.allowed_tools = set(state["allowed_tools"])
        if hasattr(self.toolbox, "load_state"):
            self.toolbox.load_state(state.get("toolbox"))
        if self.ladder is not None:
            self.ladder.floor.update(state.get("floors") or {})
        if hasattr(self.client, "set_task"):
            self.client.set_task(task_id)
        state.setdefault("results", {})
        if not state.get("response_id"):   # stopped before the first model answer: nothing ran yet
            state.update(step=1, calls=[], results={}, in_flight=None, ask=None)
            self._save(task_id, state)
            response = self._model(task_id, "plan", instructions=state["instructions"], input=goal, tools=self.tools)
            state["model"] = self.model
            return self._loop(task_id, state, response)
        ask = state.get("ask")
        if ask:
            if answer is None:   # still waiting for the user
                raise Parked("waiting_user", ask["question"], ask.get("options"), ask.get("tainted", False))
            result = {"answer": answer}
            state["unconfirmed"] = {}   # the user answered after it: their word confirms (e.g. "I did the CAPTCHA")
            state["results"][ask["call_id"]] = result
            state["ask"] = None
            self.memory.log_step(task_id, "tool_result", {"name": "ask_user", "result": result})
            self.on_event("tool", {"name": "ask_user", "args": {"question": ask["question"]}, "result": result,
                                   "badge": "you"})
            self._save(task_id, state)
        return self._loop(task_id, state, None)   # if the API dropped the old chain, _turn recaps

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
                    status = "failed" if state.get("unconfirmed") else "done"   # a reply can't confirm a change
                    return self._finish(task_id, status, summary, text, step, calls_made)
                state.update(response_id=response.id, results={}, in_flight=None, ask=None,
                             calls=[{"name": fc.name, "call_id": fc.call_id, "arguments": fc.arguments or "{}"}
                                    for fc in fcalls])
                self._save(task_id, state)
            new_turn = response is not None
            try:
                outputs, finished = self._calls(task_id, state, calls_made)
            except _Cancelled:
                return self._stop(task_id, "cancelled", "stopped by the user", step, calls_made)
            if new_turn:
                self._plan_outcome(state)
            if self.recovery is not None and not finished:
                self.recovery.end_turn()
                if self.recovery.give_up:   # impossible: stop with the report of everything tried
                    return self._stop(task_id, "failed", self.recovery.give_up, step, calls_made)
            if finished:
                status, summary = finished
                response = self._turn(task_id, state, outputs, "reply")   # the in-character closing line
                reply = getattr(response, "output_text", "") or summary
                return self._finish(task_id, status, summary, reply, step, calls_made)
            state["step"] = step + 1
            self._save(task_id, state)
            self.on_turn(task_id)
            response = self._turn(task_id, state, outputs)
        why = f"stopped after {self.max_steps} steps without finishing"
        return self._stop(task_id, "failed", self.recovery.failed_summary(why) if self.recovery else why,
                          self.max_steps, calls_made)

    def _turn(self, task_id: int, state: dict, outputs: list[dict], kind: str = "plan") -> Any:
        """The next model call on the saved chain. A recap starts a fresh chain when the ladder moves to another
        model (chaining across models isn't documented) or when the API no longer has the old chain."""
        kw = dict(instructions=state["instructions"], tools=self.tools)
        rung = None
        if self.ladder is not None:   # the closing line stays on the rung that did the work
            rung = self.rung_now if kind == "reply" and self.rung_now is not None else self.ladder.pick(kind)
        if rung is not None and rung.model and state.get("model") and rung.model != state["model"]:
            self.memory.log_step(task_id, "note", f"moved up to {rung.badge}; continuing with a recap")
            response = self._model(task_id, kind, rung, input=self._recap(task_id, state, outputs, "moved to "
                                                                          "another model"), **kw)
        else:   # live, Oct 28: the API also loses chains mid-task, not only across a restart
            state.pop("chain_check", None)
            try:
                response = self._model(task_id, kind, rung, previous_response_id=state["response_id"], input=outputs,
                                       **kw)
            except Exception as e:
                if not chain_lost(e):
                    raise
                self.memory.log_step(task_id, "note", "the model API lost this conversation (it no longer had this "
                                                      "task's earlier turns); continued with a recap")
                response = self._model(task_id, kind, rung, input=self._recap(task_id, state, outputs), **kw)
        state["model"] = self.model
        return response

    def _recap(self, task_id: int, state: dict, outputs: list[dict], why: str = "a pause") -> str:
        names = {c["call_id"]: c["name"] for c in state.get("calls", [])}
        latest = "; ".join(f"{names.get(o['call_id'], '?')} -> {o['output'][:600]}" for o in outputs)
        done = []
        for step in self.memory.steps(task_id):
            if step["kind"] == "tool_result":
                try:
                    data = json.loads(step["content"])
                except (TypeError, ValueError):
                    continue
                r = data.get("result") if isinstance(data, dict) else None
                if isinstance(r, dict) and data.get("name") == "ask_user" and "answer" in r:
                    # live, Oct 28: without the answer in the recap the model asked the same question again
                    done.append(f"ask_user (the user answered: {str(r['answer'])[:200]})")
                elif isinstance(r, dict) and data.get("name") not in (None, "finish"):
                    mark = "verified" if r.get("verified") else ("error" if r.get("error") else "ok")
                    brief = json.dumps({k: v for k, v in r.items() if k in BRIEF_KEYS}, ensure_ascii=False,
                                       default=str)[:160]
                    done.append(f"{data['name']} ({mark}{': ' + brief if brief not in ('{}', '') else ''})")
        return (f"{state['goal']}\n\n[Continuing task #{task_id} after {why}; the earlier turns are not available "
                f"here. Steps so far: {'; '.join(done[-12:]) or 'none'}. Results of the last step: {latest or 'none'}. "
                "Check before repeating anything that changes something.]")

    # ---- the ladder's records (S3) ----------------------------------------------------
    def _plan_outcome(self, state: dict) -> None:
        """A model turn did well when its calls could run: no unreadable arguments, nothing refused as a loop
        or a duplicate. (A tool failing is that tool's outcome, not the planner's.)"""
        if self.ladder is None or self._turn_meta is None or self._turn_meta[1] is None:
            return
        kind, rung, seconds, cost = self._turn_meta
        bad = [r for r in state["results"].values() if isinstance(r, dict) and (
            r.get("refused_by_recovery") or str(r.get("error", "")).startswith("could not parse arguments"))]
        self.ladder.ladder.record(kind, rung.n, not bad, cost, seconds)
        self._turn_meta = None

    def _tool_rung(self, name: str, result: Any) -> tuple[int | None, str]:
        from .ladder import tool_kind
        kind = tool_kind(name)
        if isinstance(result, dict) and isinstance(result.get("rung"), int):
            return result["rung"], kind
        return {"extract": 2, "lookup": 1, "file_op": 1, "form_fill": 3, "generate": 3, "read": 3}.get(kind), kind

    def _tool_outcome(self, name: str, result: Any, rung: int | None, kind: str) -> None:
        if self.ladder is None or rung is None or name in ("finish", "ask_user", "ask_reader") or \
                not isinstance(result, dict):
            return   # (the reader records its own outcomes: reader.py)
        from .recovery import Recovery
        cls = Recovery.classify(name, result)
        if result.get("refused_by_recovery") or cls == "needs_human":
            return   # not the rung's doing
        ok = cls is None
        if name == "fill_form":   # the planning call, and computer use for whatever the page needed beyond it
            self.ladder.ladder.record("form_fill", 3, ok, 0.0, self._tool_seconds)
            self.ladder.ladder.record("gui_action", rung, ok, 0.0, self._tool_seconds)
            if not ok:
                self.ladder.failed("gui_action", rung)
            return
        self.ladder.ladder.record(kind, rung, ok, 0.0, self._tool_seconds)
        if not ok and kind == "extract":
            self.ladder.failed(kind, rung, private=True)   # private content: never above the local rung

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
            refused = None
            if name == "finish" and parse_error is None:
                status = args.get("status") if args.get("status") in ("done", "failed") else "failed"
                summary = str(args.get("summary", "")).strip() or "(no summary)"
                before = state["results"].get(cid)
                why = next(iter((state.get("unconfirmed") or {}).values()), None) \
                    if status == "done" and before is None else None
                if isinstance(before, dict) and "error" in before:
                    pass                    # refused before a restart: the model was told, the task goes on
                elif why and not state.get("refused_done"):   # checked in code, not left to the prompt
                    refused = {"error": NOT_DONE.format(why=why)}
                    state["refused_done"] = True
                elif why:                   # asked once already: it is recorded as what it is
                    finished = ("failed", f"Not confirmed: {why}. {summary}")
                else:
                    finished = (status, summary)
            if cid in state["results"]:
                continue   # ran before this task was parked or Lantern restarted
            if self.cancel.is_set():
                raise _Cancelled()
            if parse_error is not None:
                result = parse_error
                if self.recovery is not None:
                    self.recovery.bad_arguments(name, parse_error["error"])
            elif name == "finish":
                result = refused or {"ok": True}
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
        t0 = time.monotonic()
        try:
            result = self.toolbox.call(name, args)
        except ToolError as e:
            result = {"error": str(e)}
        except AskUser as q:
            return q
        finally:
            self._tool_seconds = time.monotonic() - t0
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
        track_unconfirmed(state.setdefault("unconfirmed", {}), name, args, result)
        self._save(task_id, state)
        if name == "ask_private" and isinstance(result, dict) and "answer" in result:
            self.memory.mark_private(task_id)
        calls_made.append({"name": name, "args": args, "result": result})
        self.memory.log_step(task_id, "tool_call", {"name": name, "args": args})
        self.memory.log_step(task_id, "tool_result", {"name": name, "result": result})
        rung, kind = self._tool_rung(name, result)
        self._tool_outcome(name, result, rung, kind)
        event = {"name": name, "args": args, "result": result, "badge": self.tool_badge(name, result)}
        if rung is not None and name not in ("finish", "ask_user"):
            from .ladder import RUNGS
            event.update(rung=rung, rung_badge=RUNGS[rung].badge, kind=kind)
        self.on_event("tool", event)
