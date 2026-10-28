"""Task scheduler (S1): several tasks at once, and none of them sits idle while it waits for you.

  - N run slots (default 3, LANTERN_WORKERS). Each task has its own thread, but it needs a slot to work.
    Slots go to tasks first come, first served, and a task that gives its slot back queues at the end
    (round-robin): after every model turn a task hands its slot on if another task is waiting.
  - A task parks (gives its slot back) whenever it waits:
        waiting_user      the model called ask_user; the thread ends, and the answer starts it again
        waiting_approval  an approval card is open (the thread stays: it may hold a live browser page)
        backoff           the API said slow down (429, 5xx): the retry waits without a slot
        waiting_browser   another task is using Lantern's browser
  - Every step is checkpointed in memory.task_state (taskrun.py), so after a crash or a restart every
    unfinished task goes on by itself: queued ones start, open questions are asked again, running ones
    continue their saved turn, and recovery.py decides about a call that was cut off mid-way.
  - Each task gets its own toolbox, audited client and spending check, so taint, audit rows, receipts and
    the per-task budget never mix between tasks.
  - Events: task_parked, task_resumed (with the reason), questions (every open question, so channels can
    batch them into one message), juggling (more than one task in flight: the avatar juggles).

Threads instead of asyncio: the agent, the tools and Playwright's sync API all block. A thread per task plus
slots gives the scheduling the spec asks for without rewriting them (docs/DECISIONS.md).
"""
from __future__ import annotations

import dataclasses
import os
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable

from .approvals import PENDING, ApprovalBus, Gate
from .ladder import Ladder, TaskLadder
from .recovery import Recovery
from .taskrun import Parked, ResumableAgent

DEFAULT_WORKERS = 3
MAX_ANSWER_CHARS = 2000


class Slots:
    """N slots handed out in arrival order."""

    def __init__(self, n: int):
        if n < 1:
            raise ValueError("the scheduler needs at least one slot")
        self.n = n
        self.free = n
        self._cv = threading.Condition()
        self._waiting: deque[int] = deque()

    def acquire(self, task_id: int) -> None:
        with self._cv:
            self._waiting.append(task_id)
            while not (self.free > 0 and self._waiting[0] == task_id):
                self._cv.wait()
            self._waiting.popleft()
            self.free -= 1
            self._cv.notify_all()

    def release(self) -> None:
        with self._cv:
            self.free += 1
            self._cv.notify_all()

    def contended(self) -> bool:
        with self._cv:
            return bool(self._waiting)


@dataclass
class Run:
    id: int
    goal: str
    phase: str = "queued"        # queued | running | parked
    reason: str = ""             # parked: waiting_user | waiting_approval | backoff | waiting_browser
    state: dict | None = None    # the latest checkpoint (taskrun.py)
    question: dict | None = None
    agent: Any = None
    cancelled: bool = False
    holding: bool = False        # holds a slot
    since: float = field(default_factory=time.time)


class TaskGate(Gate):
    """Like approvals.BusGate, but the task gives its slot back while the card is open."""

    def __init__(self, sched: "Scheduler", bus: ApprovalBus, timeout: float | None = None):
        self.sched, self.bus, self.timeout = sched, bus, timeout

    def ask(self, action, summary, payload, tainted=False, **meta):
        a = self.bus.request(action, summary, payload, tainted, **meta)
        with self.sched.parked("waiting_approval", approval_id=a.id, summary=summary[:200]):
            ok = self.bus.wait(a.id, self.timeout)
        if not ok:
            if self.bus.get(a.id).status == PENDING:   # timed out waiting; don't leave it open
                self.bus.decide(a.id, a.nonce, False, by="timeout")
            self.sched.approval_closed()
            return None

        def commit(final: dict) -> None:
            self.sched.approval_closed()   # from here on it may happen: after a restart it is never re-run blindly
            self.bus.consume(a.id, final)
        return commit


class _PrivateLane:
    """The private lane for one task: one question at a time, its audit rows tagged with this task."""

    def __init__(self, router: Any, lock: threading.Lock, task_id: int):
        self._router, self._lock, self._task_id = router, lock, task_id

    def set_task(self, task_id: int | None) -> None:
        pass   # tagged per question below

    def ask_private(self, path: str, question: str) -> dict:
        with self._lock:
            self._router.set_task(self._task_id)
            try:
                return self._router.ask_private(path, question)
            finally:
                self._router.set_task(None)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._router, name)


class _SharedBrowser:
    """Lantern has one browser profile, so form fills take turns. A task waiting for the browser parks.
    While it fills, the form filler uses this task's audited, budgeted client, and the computer-use model
    the ladder picks for this task (S3: Spark first; Wonder after a fill that didn't work)."""

    def __init__(self, forms: Any, sched: "Scheduler", client: Any, ladder: Any = None):
        self._forms, self._sched, self._client, self._ladder = forms, sched, client, ladder

    def fill(self, *args, **kw):
        lock = self._sched._browser
        if not lock.acquire(blocking=False):
            with self._sched.parked("waiting_browser"):
                lock.acquire()
        try:
            saved = self._forms.client, getattr(self._forms, "settings", None)
            self._forms.client = self._client
            rung = self._ladder.pick("gui_action") if self._ladder is not None and saved[1] is not None else None
            if rung is not None:
                self._forms.settings = dataclasses.replace(saved[1], model=rung.model)
            try:
                result = self._forms.fill(*args, **kw)
            finally:
                self._forms.client = saved[0]
                if saved[1] is not None:
                    self._forms.settings = saved[1]
            if rung is not None and isinstance(result, dict):
                result = {**result, "rung": rung.n}
            return result
        finally:
            lock.release()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._forms, name)


def raw_client(client: Any) -> Any:
    """The model client under Core's retry and audit wrappers (each task gets its own wrappers)."""
    from .audit import GuardedClient
    from .reliability import RetryingClient
    while isinstance(client, (RetryingClient, GuardedClient)):
        client = client.inner
    return client


class Scheduler:
    def __init__(self, core: Any, workers: int = DEFAULT_WORKERS, *, approval_timeout: float | None = None,
                 retry: dict | None = None):
        self.core = core
        self.workers = workers
        self.slots = Slots(workers)
        self.approval_timeout = approval_timeout
        self.retry = dict(retry or {})        # RetryingClient options (tests make backoff fast)
        self.recovery_limits = None           # recovery.Limits (tests make waits short)
        self.ladder = Ladder(core.memory)     # S3: which rung does each kind of step well
        # S0: the Planner/Reader split and labels (security.py). LANTERN_READER=off only to compare, never by default.
        self.reader_split = os.environ.get("LANTERN_READER", "on").strip().lower() != "off"
        self.runs: dict[int, Run] = {}
        self.closing = threading.Event()
        self._lock = threading.RLock()
        self._local = threading.local()
        self._approvals: dict[str, int] = {}  # approval id -> task id, for receipts and events
        self._browser = threading.Lock()
        self._private = threading.Lock()
        self._pending: list[tuple[Run, Callable]] = []
        self._last_juggle: tuple | None = None
        self._last_questions: tuple | None = None
        core.bus.subscribe(self._on_approval)   # before Core's own listener, so the map is ready for it

    # ---- which task is this thread working for -------------------------------------
    def current(self) -> int | None:
        return getattr(self._local, "task_id", None)

    def task_for_approval(self, approval_id: str) -> int | None:
        with self._lock:
            return self._approvals.get(approval_id)

    def _on_approval(self, kind: str, approval) -> None:
        if kind == "requested" and (task_id := self.current()) is not None:
            with self._lock:
                self._approvals[approval.id] = task_id

    def active(self, task_id: int) -> bool:
        with self._lock:
            return task_id in self.runs

    # ---- submitting ------------------------------------------------------------------
    def submit(self, task_id: int, goal: str) -> None:
        run = Run(task_id, goal)
        with self._lock:
            self.runs[task_id] = run
        self.core.memory.save_task_state(task_id, "queued", "", {"goal": goal})
        self._start(run, lambda agent: self._goal(run, agent, goal))
        self._juggling()

    def submit_job(self, task_id: int, goal: str, job: Callable[[ResumableAgent], Any]) -> None:
        """Work that isn't an agent run (learning a routine) but still wants a slot, a client and a budget."""
        run = Run(task_id, goal)
        with self._lock:
            self.runs[task_id] = run
        self.core.memory.save_task_state(task_id, "queued", "", {"goal": goal, "job": True})
        self._start(run, job)
        self._juggling()

    def _goal(self, run: Run, agent: ResumableAgent, goal: str) -> Any:
        """A learned routine if one matches (S4, rung 0), else the agent. Afterwards, offer to learn it."""
        compiler = getattr(self.core, "compiler", None)
        if compiler is None:
            return agent.run(goal, task_id=run.id)
        match = compiler.match(goal, client=agent.client, ladder=agent.ladder, task_id=run.id)
        if match is not None:
            return compiler.run_task(run.id, goal, match, agent=agent, sched=self)
        result = agent.run(goal, task_id=run.id)
        if getattr(result, "status", None) == "done":
            try:
                offer = compiler.offer(run.id)
            except Exception:   # an offer is a nicety; it must never fail a finished task
                offer = None
            if offer is not None:
                self.core.emit({"type": "learn_offer", "task_id": run.id, "goal": goal, "auto": offer == "auto"})
                if offer == "auto":   # the same goal done before: learn it without asking
                    self.core.learn(run.id, auto=True)
        return result

    @contextmanager
    def browser(self):
        """Lantern's one browser profile, for as long as the block runs; a task waiting for it parks."""
        if not self._browser.acquire(blocking=False):
            with self.parked("waiting_browser"):
                self._browser.acquire()
        try:
            yield
        finally:
            self._browser.release()

    def recover(self) -> set[int]:
        """After a restart: take back every unfinished task with a saved state. Threads start in start()."""
        keep: set[int] = set()
        memory = self.core.memory
        for row in memory.task_states():
            task_id, st = row["task_id"], row["state"] or {}
            try:
                task = memory.get_task(task_id)
            except KeyError:
                memory.delete_task_state(task_id)
                continue
            if task.status != "active":
                memory.delete_task_state(task_id)
                continue
            if st.get("job") or (st.get("routine") and st.get("acted")):
                # Learning: just say it again. A routine that had already submitted: never run it twice.
                why = ("stopped by a restart; ask Lantern to learn it again" if st.get("job") else
                       f"Lantern stopped while the routine '{st['routine']}' was running, after it had submitted "
                       "something; nothing was repeated. Check the site before trying again.")
                memory.log_step(task_id, "note", why)
                memory.finish_task(task_id, "failed", why)
                memory.delete_task_state(task_id)
                continue
            keep.add(task_id)
            run = Run(task_id, task.goal, state=st if st.get("instructions") else None)
            with self._lock:
                self.runs[task_id] = run
            memory.log_step(task_id, "note", "Lantern restarted; this task continues where it was")
            if row["reason"] == "waiting_user" and st.get("ask"):
                run.phase, run.reason, run.question = "parked", "waiting_user", st["ask"]
            elif run.state is None:
                self._pending.append((run, lambda agent, r=run, g=task.goal: self._goal(r, agent, g)))
            else:
                self._pending.append((run, lambda agent, s=st, t=task_id: agent.resume(t, s)))
        return keep

    def start(self) -> None:
        pending, self._pending = self._pending, []
        for run, body in pending:
            self._start(run, body)
        self._juggling()
        self._questions_changed()

    def _start(self, run: Run, body: Callable[[ResumableAgent], Any]) -> None:
        threading.Thread(target=self._thread, args=(run, body), name=f"lantern-task-{run.id}", daemon=True).start()

    def _thread(self, run: Run, body: Callable[[ResumableAgent], Any]) -> None:
        self._local.task_id, self._local.run = run.id, run
        finished = True
        try:
            self._acquire(run)
            if run.cancelled:
                self._cancel(run, "stopped before it started")
                return
            run.agent = self._agent(run)
            body(run.agent)
        except Parked as p:
            finished = False
            if p.reason == "waiting_user":
                self._waiting_user(run, p)
            else:   # shutting down: the checkpoint stays, the task continues on the next start
                with self._lock:
                    self.runs.pop(run.id, None)
        except Exception as e:
            if run.agent is None:   # failed before the agent could start (it marks its own failures)
                self._fail(run, e)
        finally:
            if run.holding:
                run.holding = False
                self.slots.release()
            if finished:
                with self._lock:
                    self.runs.pop(run.id, None)
                self.core.memory.delete_task_state(run.id)
            run.agent = None
            self._juggling()

    def _acquire(self, run: Run) -> None:
        self.slots.acquire(run.id)
        with self._lock:
            run.holding, run.phase, run.reason = True, "running", ""

    # ---- one task's own toolbox, client and agent -------------------------------------
    def _agent(self, run: Run) -> ResumableAgent:
        from .audit import GuardedClient
        from .budget import BudgetGuard
        from .reliability import RetryingClient
        from .tools import Toolbox
        core = self.core
        guarded = GuardedClient(raw_client(core.client), core.audit, destination=core.cfg.base_url)
        budget = BudgetGuard(core.memory, core.budget.limits, lambda: run.id, core.emit)
        client = RetryingClient(guarded, breaker=getattr(core.client, "breaker", None),
                                sleep=lambda s: self._backoff(run, s),
                                on_retry=lambda info: core.emit({"type": "retry", "task_id": run.id, **info}),
                                before_call=budget.check, **self.retry)
        events = self._events(run.id)
        limit = core.budget.limits.task
        ladder = TaskLadder(self.ladder, budget_left=lambda: None if limit is None else
                            limit - core.memory.task_usage(run.id)["cost_usd"])
        lane = _PrivateLane(core.router, self._private, run.id)
        parts = dict(router=lane, forms=_SharedBrowser(core.forms, self, client, ladder) if core.forms is not None else None,
                     email=core.email, envmap=core.envmap, widgets=core.widgets, mcp=core.mcp)
        gate = TaskGate(self, core.bus, self.approval_timeout)
        if self.reader_split:   # S0: the planner never sees raw untrusted content; private values never leave
            from .reader import Reader
            from .security import GuardedToolbox
            reader = Reader(client, core.memory, task_id=run.id, router=lane, ladder=ladder, emit=core.emit)
            toolbox = GuardedToolbox(core.policy, core.memory, gate, reader=reader, **parts)
            guarded.deny = toolbox.handles.deny
        else:
            toolbox = Toolbox(core.policy, core.memory, gate, **parts)
        toolbox.client = client
        toolbox.command_runner = getattr(core.toolbox, "command_runner", None)
        toolbox.on_event = events
        agent = ResumableAgent(client, core.memory, toolbox, model=core.agent.model, persona=core.agent.persona,
                               max_steps=core.agent.max_steps, on_event=events, checkpoint=self._checkpoint(run),
                               on_turn=self._on_turn, ladder=ladder)
        agent.recovery = Recovery(core.memory, run.id, emit=core.emit, policy=core.policy,
                                  limits=self.recovery_limits, sleep=lambda s: self._backoff(run, s),
                                  rung=lambda: agent.rung_now.n if agent.rung_now is not None else None,
                                  escalate=agent.escalate)
        return agent

    def _events(self, task_id: int) -> Callable[[str, dict], None]:
        from .core import _short

        def emit(kind: str, data: dict) -> None:
            event = {"type": kind, **data}
            event.setdefault("task_id", task_id)
            if kind == "tool":
                event["result"] = _short(data.get("result"))
            self.core.emit(event)
        return emit

    def _checkpoint(self, run: Run) -> Callable[[int, dict, str, str], None]:
        def save(task_id: int, state: dict, phase: str, reason: str) -> None:
            run.state = state
            self.core.memory.save_task_state(task_id, phase, reason, state)
        return save

    def _on_turn(self, task_id: int) -> None:
        """Between model turns: stop for a shutdown, honour STOP, and hand the slot on if someone waits."""
        run = self._local.run
        if self.closing.is_set():
            raise Parked("shutdown")
        if run.cancelled and run.agent is not None:
            run.agent.cancel.set()
        if self.slots.contended():
            run.holding = False
            self.slots.release()
            self._acquire(run)

    # ---- parking while the thread waits ---------------------------------------------
    @contextmanager
    def parked(self, reason: str, **info):
        """Give the slot back while waiting; get one again (at the back of the line) afterwards."""
        run: Run | None = getattr(self._local, "run", None)
        if run is None:   # not a scheduled task (e.g. an undo job on Core's own worker)
            yield
            return
        with self._lock:
            run.phase, run.reason, run.since = "parked", reason, time.time()
        if run.holding:
            run.holding = False
            self.slots.release()
        if run.state is not None:
            if reason == "waiting_approval":
                run.state["approval_open"] = True   # nothing happens before the yes: after a restart, ask again
            self.core.memory.save_task_state(run.id, "parked", reason, run.state)
        self._emit("task_parked", run, reason=reason, **info)
        self._juggling()
        try:
            yield
        finally:
            self._acquire(run)
            if run.state is not None:
                self.core.memory.save_task_state(run.id, "running", "", run.state)
            self._emit("task_resumed", run, reason=reason)
            self._juggling()

    def approval_closed(self) -> None:
        run: Run | None = getattr(self._local, "run", None)
        if run is not None and run.state is not None and run.state.get("approval_open"):
            run.state["approval_open"] = False
            self.core.memory.save_task_state(run.id, run.phase, run.reason, run.state)

    def _backoff(self, run: Run, seconds: float) -> None:
        with self.parked("backoff", wait_s=round(seconds, 2)):
            if run.agent is not None:
                run.agent.cancel.wait(seconds)
            else:
                time.sleep(seconds)

    # ---- questions for the user ---------------------------------------------------------
    def _waiting_user(self, run: Run, p: Parked) -> None:
        with self._lock:
            run.phase, run.reason, run.since = "parked", "waiting_user", time.time()
            run.question = {"question": p.question, "options": p.options, "tainted": p.tainted}
        self.core.memory.log_step(run.id, "note", f"waiting for your answer: {p.question}")
        self._emit("task_parked", run, reason="waiting_user", question=p.question, options=p.options,
                   tainted=p.tainted)
        self._questions_changed()

    def questions(self) -> list[dict]:
        """Every open question, oldest first."""
        with self._lock:
            runs = [r for r in self.runs.values() if r.phase == "parked" and r.reason == "waiting_user"]
        return [{"task_id": r.id, "goal": r.goal.split("\n")[0][:120], "question": r.question["question"],
                 "options": list(r.question.get("options") or []), "tainted": bool(r.question.get("tainted")),
                 "since": r.since} for r in sorted(runs, key=lambda r: r.since)]

    def answer(self, task_id: int, text: str, by: str = "user") -> None:
        """The user's answer: the task queues for a slot and continues the same step with it."""
        text = (text or "").strip()[:MAX_ANSWER_CHARS]
        if not text:
            raise ValueError("the answer is empty")
        with self._lock:
            run = self.runs.get(task_id)
            if run is None or run.phase != "parked" or run.reason != "waiting_user":
                raise ValueError(f"task #{task_id} isn't waiting for an answer")
            run.phase, run.reason, run.question, run.cancelled = "queued", "", None, False
        state = run.state if run.state is not None else self.core.memory.load_task_state(task_id)["state"]
        self.core.memory.log_step(task_id, "note", f"answered on {by}")
        self._emit("task_resumed", run, reason="answered", by=by)
        self._questions_changed()
        self._start(run, lambda agent: agent.resume(task_id, state, answer=text))

    # ---- stopping ------------------------------------------------------------------------
    def stop_all(self) -> list[int]:
        """STOP: every task stops at its next step; open questions are withdrawn."""
        with self._lock:
            runs = list(self.runs.values())
        for r in runs:
            r.cancelled = True
            if r.agent is not None:
                r.agent.cancel.set()
            if r.phase == "parked" and r.reason == "waiting_user":
                self._cancel(r, "stopped by the user")
        return [r.id for r in runs]

    def stop_one(self, task_id: int) -> bool:
        """"stop #N": only task N stops at its next step; its open question is withdrawn and its open approval
        cards are denied. The other tasks carry on. False if N isn't running, queued or parked."""
        with self._lock:
            r = self.runs.get(task_id)
        if r is None:
            return False
        r.cancelled = True
        if r.agent is not None:
            r.agent.cancel.set()
        for a in self.core.bus.pending():
            if self.task_for_approval(a.id) == task_id:
                try:
                    self.core.bus.decide(a.id, a.nonce, False, by="stopped")
                except Exception:   # answered or expired in the meantime
                    pass
        if r.phase == "parked" and r.reason == "waiting_user":
            self._cancel(r, "stopped by the user")
        return True

    def _cancel(self, run: Run, why: str) -> None:
        with self._lock:
            self.runs.pop(run.id, None)
        self.core.memory.log_step(run.id, "note", why)
        self.core.memory.finish_task(run.id, "cancelled", why)
        self.core.memory.delete_task_state(run.id)
        self.core.emit({"type": "done", "id": run.id, "task_id": run.id, "status": "cancelled", "summary": why,
                        "reply": why, "cost_usd": self.core.memory.task_usage(run.id)["cost_usd"], "steps": 0})
        self._questions_changed()
        self._juggling()

    def _fail(self, run: Run, e: Exception) -> None:
        from .reliability import explain
        why = explain(e)
        self.core.memory.log_step(run.id, "note", f"error: {type(e).__name__}: {e}")
        self.core.memory.finish_task(run.id, "failed", why)
        self.core.emit({"type": "done", "id": run.id, "task_id": run.id, "status": "failed", "summary": why,
                        "reply": f"I couldn't finish that. {why}", "cost_usd": 0.0, "steps": 0})

    def shutdown(self) -> None:
        """Lantern is closing: running tasks stop at their next turn and keep their checkpoint."""
        self.closing.set()

    # ---- status and events -------------------------------------------------------------
    def busy(self) -> bool:
        """Something is running or will run without the user (a question waiting for them doesn't count)."""
        with self._lock:
            return any(r.phase != "parked" or r.reason != "waiting_user" for r in self.runs.values())

    def snapshot(self) -> dict:
        with self._lock:
            runs = sorted(self.runs.values(), key=lambda r: r.id)
            return {"running": [r.id for r in runs if r.phase == "running"],
                    "queued": [r.id for r in runs if r.phase == "queued"],
                    "parked": [{"task_id": r.id, "reason": r.reason} for r in runs if r.phase == "parked"],
                    "workers": self.workers}

    def _emit(self, kind: str, run: Run, **data) -> None:
        self.core.emit({"type": kind, "task_id": run.id, **data})

    def _juggling(self) -> None:
        snap = self.snapshot()
        key = (tuple(snap["running"]), tuple(snap["queued"]), tuple((p["task_id"], p["reason"]) for p in snap["parked"]))
        with self._lock:
            if key == self._last_juggle:
                return
            self._last_juggle = key
        active = len(snap["running"]) + len(snap["queued"]) + len(snap["parked"])
        self.core.emit({"type": "juggling", "on": active > 1, "active": active, **snap})

    def _questions_changed(self) -> None:
        items = self.questions()
        key = tuple(q["task_id"] for q in items)
        with self._lock:
            if key == self._last_questions:
                return
            self._last_questions = key
        self.core.emit({"type": "questions", "items": items})
