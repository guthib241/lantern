"""Lantern core: wires memory, policy, scrubber/audit, router, approvals and the
agent together, and runs tasks one at a time on a worker thread.

The server (server.py), the channels (WhatsApp/Telegram) and the CLI all talk to
one `Core`. Events from the agent and the approval bus are fanned out to
subscribers (the server's websocket, channels).
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import config as config_mod
from . import models
from .agent import Agent
from .approvals import ApprovalBus, BusGate
from .audit import AuditLog, GuardedClient
from .budget import BudgetGuard, Limits
from .crypto import cipher_for
from .email_tool import EmailService, EmailSettings
from .envmap import EnvMap
from .forms import FormService, FormSettings
from .mcp_client import MCPHub
from .memory import Memory
from .policy import Policy, PolicyError
from .reliability import RetryingClient
from .router import Router, RouterConfig, make_backend
from .sam import CheckIn
from .tools import Toolbox, ToolError
from .undo import UNDONE, UndoError, undo_plan, undoable
from .widgets import Widgets

EventSink = Callable[[dict], None]


class NoModelClient:
    """Stands in when MODEL_API_KEY is missing, so the server and UI still start."""

    class _R:
        def create(self, **kw):
            raise RuntimeError(f"set {models.API_KEY_ENV} to run tasks (or start with --demo)")

    def __init__(self):
        self.responses = self._R()


INTERRUPTED = "cut short when Lantern stopped; say 'resume' to continue"
RESUME_NOTE = "\n\n[Resuming task #"


def recover_interrupted(memory: Memory, keep: set[int] | frozenset = frozenset()) -> list:
    """Tasks still 'active' from a previous run were cut short (crash, restart, power): mark them.
    `keep`: tasks the scheduler picks up again from their checkpoint (scheduler.py)."""
    out = []
    for t in memory.active_tasks():
        if t.id in keep:
            continue
        memory.log_step(t.id, "note", "interrupted: Lantern stopped before this task finished")
        memory.finish_task(t.id, "interrupted", INTERRUPTED)
        out.append(memory.get_task(t.id))
    return out


def resume_goal(memory: Memory, task) -> str:
    """The goal again, plus what the cut-short run already did, so nothing gets done twice."""
    goal = task.goal.split(RESUME_NOTE)[0]
    done = []
    for step in memory.steps(task.id):
        if step["kind"] != "tool_result":
            continue
        try:
            data = json.loads(step["content"])
        except (TypeError, ValueError):
            continue
        name, result = data.get("name"), data.get("result") or {}
        if name in (None, "finish") or not isinstance(result, dict):
            continue
        state = ("verified" if result.get("verified") else "error: " + str(result["error"])[:60] if result.get("error")
                 else "not verified")
        done.append(f"{name} ({state})")
    already = "; ".join(done[-12:]) if done else "nothing yet"
    return (f"{goal}{RESUME_NOTE}{task.id}, which was cut short when Lantern stopped. Already done then: {already}. "
            "Check before repeating anything that changes something.]")


class Core:
    def __init__(self, cfg: config_mod.Config, client: Any = None, *, memory: Memory | None = None,
                 audit: AuditLog | None = None, policy: Policy | None = None, router: Router | None = None,
                 bus: ApprovalBus | None = None, approval_timeout: float | None = None,
                 forms: FormService | None = None, email: EmailService | None = None,
                 mcp: MCPHub | None = None, reminder_tick: float = 1.0, workers: int = 0):
        self.cfg = cfg
        cipher = None if (memory and audit) else cipher_for(cfg)
        self.memory = memory or Memory(cfg.db_path, cipher)
        self.audit = audit or AuditLog(cfg.audit_path, cipher)
        self.policy = policy or Policy.load(cfg.zones_file, cfg.roots)
        self.router = router or Router(self.policy, make_backend(RouterConfig.from_env(dict(os.environ)), self.audit))
        self.bus = bus or ApprovalBus()
        if client is None:
            client = self._default_client()
        guarded = client if isinstance(client, GuardedClient) else GuardedClient(client, self.audit,
                                                                                 destination=cfg.base_url)
        # Retries wrap the audited client, so every attempt that leaves the laptop is audited. The spending
        # limits are checked before each call, in code: a refused call never leaves and is never retried.
        self.current_task: int | None = None
        self.budget = BudgetGuard(self.memory, Limits.from_env(dict(os.environ)), lambda: self.current_task, self.emit)
        self.client = RetryingClient(guarded, on_retry=lambda info: self.emit({"type": "retry", **info}),
                                     before_call=self.budget.check)
        self.forms = forms or FormService(FormSettings.from_config(cfg), self.client, self.policy, self.memory)
        self.email = email or EmailService(EmailSettings.from_env(dict(os.environ)), self.policy, self.audit,
                                           outbox_dir=Path(cfg.media_dir) / "outbox")
        self.envmap = EnvMap(self.policy, cfg.envmap_path)
        self.widgets = Widgets(Path(cfg.widgets_dir))
        self.mcp = mcp if mcp is not None else MCPHub.from_file(cfg.mcp_config).start()
        self.toolbox = Toolbox(self.policy, self.memory, BusGate(self.bus, approval_timeout), router=self.router,
                               forms=self.forms, email=self.email, envmap=self.envmap, widgets=self.widgets,
                               mcp=self.mcp)
        self.toolbox.client = self.client
        self.checkin = CheckIn(self.client, self.memory, self.emit)
        self.toolbox.on_event = self._agent_event
        self.agent = Agent(self.client, self.memory, self.toolbox, model=cfg.model, persona=cfg.persona,
                           on_event=self._agent_event)
        self._sinks: list[EventSink] = []
        self._queue: queue.Queue = queue.Queue()
        self._undoing: set[int] = set()   # tasks with an undo queued (a double tap queues one)
        self._undo_lock = threading.Lock()
        self._teaching: dict[int, dict] = {}   # S8: teaching task -> {"stop": Event, "cancelled": bool}
        self.scheduler = None   # S1: with workers > 0, up to that many tasks run at once (scheduler.py)
        self.compiler = None    # S4: learned routines (compiler.py), with the scheduler
        if workers > 0:
            from .compiler import Compiler
            from .scheduler import Scheduler
            self.scheduler = Scheduler(self, workers, approval_timeout=approval_timeout)
            self.compiler = Compiler(self)
        keep = self.scheduler.recover() if self.scheduler is not None else set()
        self.interrupted = recover_interrupted(self.memory, keep)   # before the worker starts taking tasks
        self.bus.subscribe(self._approval_event)
        self._worker = threading.Thread(target=self._work, name="lantern-agent", daemon=True)
        self._worker.start()
        self._closing = threading.Event()
        self.reminder_tick = reminder_tick
        if reminder_tick > 0:
            threading.Thread(target=self._remind_loop, name="lantern-reminders", daemon=True).start()
        if self.scheduler is not None:
            self.scheduler.start()   # tasks saved before a restart continue

    def _default_client(self):
        key = os.environ.get(models.API_KEY_ENV)
        if not key:
            return NoModelClient()
        from openai import OpenAI
        # The SDK's silent retries are off: RetryingClient retries visibly (events) with a circuit breaker.
        return OpenAI(base_url=self.cfg.base_url, api_key=key, timeout=self.cfg.timeout, max_retries=0)

    # ---- events ----------------------------------------------------------------
    def subscribe(self, sink: EventSink) -> Callable[[], None]:
        self._sinks.append(sink)
        return lambda: self._sinks.remove(sink) if sink in self._sinks else None

    def emit(self, event: dict) -> None:
        event.setdefault("ts", time.time())
        for sink in list(self._sinks):
            try:
                sink(event)
            except Exception:
                pass

    def _agent_event(self, kind: str, data: dict) -> None:
        event = {"type": kind, **data}
        if self.current_task is not None:
            event.setdefault("task_id", self.current_task)
        if kind == "tool":
            event["result"] = _short(data.get("result"))
        self.emit(event)

    def _approval_event(self, kind: str, approval) -> None:
        task_id = self.current_task
        if self.scheduler is not None and (owner := self.scheduler.task_for_approval(approval.id)) is not None:
            task_id = owner
        if kind in ("decided", "expired", "used") and task_id is not None:   # for the task's receipt
            self.memory.log_step(task_id, "approval", {
                "id": approval.id, "event": kind, "action": approval.action, "summary": approval.summary,
                "payload_hash": approval.payload_hash, "status": approval.status, "by": approval.decided_by,
                "tainted": approval.tainted})
        self.emit({"type": "approval", "event": kind, "approval": approval.public(include_nonce=(kind == "requested")),
                   "task_id": task_id})

    # ---- tasks -----------------------------------------------------------------
    def submit(self, goal: str) -> int:
        goal = goal.strip()
        if not goal:
            raise ValueError("goal is empty")
        task = self.memory.start_task(goal)
        if self.scheduler is not None:
            self.emit({"type": "queued", "task_id": task.id, "goal": goal})
            self.scheduler.submit(task.id, goal)
            return task.id
        self._queue.put((task.id, goal, None))
        self.emit({"type": "queued", "task_id": task.id, "goal": goal})
        return task.id

    def answer(self, task_id: int, text: str, by: str = "user") -> None:
        """The user's answer to a task's question (ask_user); the task continues where it stopped."""
        if self.scheduler is None:
            raise ValueError("no task is waiting for an answer")
        self.scheduler.answer(task_id, text, by)

    def questions(self) -> list[dict]:
        return self.scheduler.questions() if self.scheduler is not None else []

    # ---- learned routines (S4, compiler.py) ------------------------------------------------
    def learn(self, task_id: int, auto: bool = False, replace: str | None = None) -> int:
        """Compile a finished, verified task into a routine, as a task of its own (its model calls are counted).
        `replace`: the routine this task's run supersedes (S5: the site's flow changed)."""
        if self.compiler is None:
            raise ValueError("learning routines needs Lantern's scheduler (lantern --serve)")
        source = self.memory.get_task(task_id)   # KeyError if unknown
        if not self.compiler.learnable(task_id):
            try:
                self.compiler.skeleton(task_id)
            except Exception as e:
                raise ValueError(f"task #{task_id} can't be learned: {e}") from e
        goal = (f"Re-learn {replace} from task #{task_id}" if replace else
                f"Learn task #{task_id} as a routine: {source.goal.splitlines()[0][:80]}")
        job = self.memory.start_task(goal)
        self.memory.log_step(job.id, "plan", {"goal": goal, "learn_from": task_id, "auto": auto, "replace": replace})
        self.emit({"type": "queued", "task_id": job.id, "goal": goal})
        self.scheduler.submit_job(job.id, goal, lambda agent: self._learn_job(agent, job.id, goal, task_id, replace))
        return job.id

    def _learn_job(self, agent, job_id: int, goal: str, task_id: int, replace: str | None = None) -> None:
        from .compiler import CompileError
        agent.cancel.clear()
        agent.toolbox.begin_task(job_id, goal)
        if hasattr(agent.client, "set_task"):
            agent.client.set_task(job_id)
        agent.on_event("task", {"id": job_id, "goal": goal})
        try:
            info = self.compiler.learn(task_id, client=agent.client, ladder=agent.ladder, job_id=job_id, replace=replace)
        except (CompileError, KeyError, ValueError) as e:
            status, summary = "failed", f"Couldn't learn task #{task_id}: {e}"
        except Exception as e:   # never leave the learning task hanging as "active"
            from .reliability import explain
            self.memory.log_step(job_id, "note", f"error: {type(e).__name__}: {e}")
            status, summary = "failed", f"Couldn't learn task #{task_id}: {explain(e)}"
        else:
            status = "done"
            summary = (f"{'Re-learned' if replace else 'Learned'} '{info['name']}' v{info['version']}: {info['steps']} steps"
                       + (f", with {', '.join(info['params'])} to choose" if info["params"] else "")
                       + (f". Verified: {info['verify']}. Next time it runs with no model calls."
                          if info["trusted"] else f". Not verified yet ({info['verify']}), so it won't run by itself."))
        self.memory.log_step(job_id, "reply", summary)
        self.memory.finish_task(job_id, status, summary)
        agent.on_event("done", {"id": job_id, "status": status, "summary": summary, "reply": summary,
                                "cost_usd": self.memory.task_usage(job_id)["cost_usd"], "steps": 1})

    # ---- teach once by demonstration (S8, recorder.py) ------------------------------------------------
    def teach(self, url: str, goal: str, *, act: Callable | None = None, driver_factory: Callable | None = None) -> int:
        """Open Lantern's browser on `url` and record the user doing `goal` once; then learn it (S4).
        Returns the teaching task. `teach_stop` ends the recording (the Done button)."""
        from .forms.filler import url_problem
        if self.compiler is None:
            raise ValueError("teaching needs Lantern's scheduler (lantern --serve)")
        url, goal = str(url).strip(), " ".join(str(goal).split())[:200]
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError("give the web address where the task starts (http or https)")
        if not goal:
            raise ValueError("say what the task is, for example 'apply for a city permit'")
        if problem := url_problem(url, self.forms.settings):
            raise ValueError(problem)
        if self._teaching:
            raise ValueError("Lantern is already watching you do a task; press Done (or STOP) first")
        job = self.memory.start_task(goal)
        session = {"stop": threading.Event(), "cancelled": False}
        self._teaching[job.id] = session
        self.memory.log_step(job.id, "plan", {"goal": goal, "teach": url})
        self.emit({"type": "queued", "task_id": job.id, "goal": goal})
        self.scheduler.submit_job(job.id, goal, lambda agent: self._teach_job(agent, job.id, goal, url, session, act,
                                                                              driver_factory))
        return job.id

    def teach_stop(self, task_id: int | None = None) -> bool:
        """Done: stop recording (one teaching task, or whichever is running)."""
        hits = [s for t, s in self._teaching.items() if task_id is None or t == task_id]
        for s in hits:
            s["stop"].set()
        return bool(hits)

    def teach_note(self, task_id: int | None, text: str) -> bool:
        """Narration while teaching ("this is my passport number"): it helps name and describe the routine."""
        text = " ".join(str(text).split())[:300]
        ids = [t for t in self._teaching if task_id is None or t == task_id]
        if not text or not ids:
            return False
        self.memory.log_step(ids[0], "narration", text)
        self.emit({"type": "teach", "task_id": ids[0], "phase": "note"})
        return True

    def _teach_driver(self):
        from .forms.driver import BrowserDriver, chromium_executable
        st = self.forms.settings
        headless = os.environ.get("LANTERN_TEACH_HEADLESS", "").strip().lower() in ("1", "true", "yes")
        return BrowserDriver(st.profile_dir, headless=headless, executable_path=chromium_executable(st.chromium),
                             guard_submits=False)   # the user drives: their own clicks submit

    def _teach_job(self, agent, job_id: int, goal: str, url: str, session: dict, act, factory) -> None:
        from .recorder import TeachError, record, traces_from
        from .reliability import explain
        agent.toolbox.begin_task(job_id, goal)
        agent.on_event("task", {"id": job_id, "goal": goal})
        seen = {"n": 0}

        def on_event(ev: dict) -> None:   # the panel shows that it's watching; values never go in events
            if ev.get("kind") in ("field", "click"):
                seen["n"] += 1
                self.emit({"type": "teach", "task_id": job_id, "phase": "event", "kind": ev["kind"],
                           "count": seen["n"]})
        self.emit({"type": "teach", "task_id": job_id, "phase": "recording", "url": url})
        traces: list = []
        try:
            with self.scheduler.browser():
                rec = record(factory or self._teach_driver, url, stop=session["stop"], act=act, on_event=on_event,
                             confirm_selectors=self.forms.settings.confirm_selectors)
            if session["cancelled"]:
                raise TeachError("stopped; nothing was kept")
            traces = traces_from(rec, self.policy)
            left = sorted({x for t in traces for x in t.get("left_for_you") or []})
            if left:
                raise TeachError("the form has fields only you may fill (" + ", ".join(left[:3]) + "), so a "
                                 "routine can't do it for you; nothing was kept")
        except TeachError as e:
            status, summary, traces = ("cancelled" if session["cancelled"] else "failed"), f"Not learned: {e}.", []
        except Exception as e:   # the browser closed, or wouldn't start
            status, summary, traces = "failed", f"The recording stopped: {explain(e)}", []
        else:
            fields = sum(len(t["fields"]) for t in traces)
            status = "done"
            summary = (f"You showed me how to {goal}: {len(traces)} page{'s' if len(traces) != 1 else ''}, "
                       f"{fields} field{'s' if fields != 1 else ''}, {rec.seconds:.0f} s. Learning it now.")
        finally:
            self._teaching.pop(job_id, None)
        for t in traces:
            self.memory.log_step(job_id, "trace", t)
        self.emit({"type": "teach", "task_id": job_id, "phase": "stopped", "status": status})
        self.memory.log_step(job_id, "reply", summary)
        self.memory.finish_task(job_id, status, summary)
        agent.on_event("done", {"id": job_id, "status": status, "summary": summary, "reply": summary,
                                "cost_usd": self.memory.task_usage(job_id)["cost_usd"], "steps": len(traces)})
        if status == "done":
            try:
                self.learn(job_id, auto=True)
            except ValueError as e:
                self.memory.log_step(job_id, "note", f"couldn't learn it: {e}")
                self.emit({"type": "teach", "task_id": job_id, "phase": "not_learned", "reason": str(e)[:200]})

    # ---- glasses -> laptop handoff (S9, handoff.py) and photo to scan (S10, scan.py) ----------------------
    def photo(self, image: bytes, instruction: str = "") -> int:
        """A photo from the phone (or the glasses): scan it, read it, and run the learned routine it's for with the
        scan attached (after the user approves). `instruction`: the user's own words, if any. Returns the task."""
        if self.compiler is None:
            raise ValueError("photos need Lantern's scheduler (lantern --serve)")
        instruction = " ".join(str(instruction or "").split())[:200]
        goal = f"Scan this photo{': ' + instruction if instruction else ''}"
        job = self.memory.start_task(goal)
        self.memory.log_step(job.id, "plan", {"goal": goal, "photo_bytes": len(image)})
        self.emit({"type": "queued", "task_id": job.id, "goal": goal})
        self.scheduler.submit_job(job.id, goal, lambda agent: self._photo_job(agent, job.id, goal, image, instruction))
        return job.id

    def scans_folder(self) -> Path:
        """Where scans go: a Scans folder in the first writable zone."""
        z = next((z for z in self.policy.describe() if z["zone"] == "writable"), None)
        if z is None:
            raise ValueError("there's no writable folder to save scans in (lantern --set-zone FOLDER writable)")
        return Path(z["path"]) / "Scans"

    def _photo_job(self, agent, job_id: int, goal: str, image: bytes, instruction: str) -> None:
        from . import handoff
        from .reliability import explain
        from .scan import ScanError, file_name, scan
        agent.toolbox.begin_task(job_id, goal)
        if hasattr(agent.client, "set_task"):
            agent.client.set_task(job_id)
        agent.on_event("task", {"id": job_id, "goal": goal})
        try:
            sc = scan(image, client=agent.client, policy=self.policy, folder=self.scans_folder(), memory=self.memory,
                      task_id=job_id)
            paper = handoff.read(agent.client, self.policy.read_bytes(sc["png"]), memory=self.memory, task_id=job_id)
            if paper["kind"]:   # a name that says what it is: "2026-10-28 permit renewal notice.pdf"
                for ext in ("png", "pdf"):
                    target = Path(sc[ext]).with_name(f"{file_name(paper['kind'])}.{ext}")
                    if not self.policy.exists(target):
                        self.policy.move(sc[ext], target)
                        sc[ext] = str(target)
            self.memory.log_step(job_id, "tool_call", {"name": "scan", "args": {"bytes": len(image)}})
            self.memory.log_step(job_id, "tool_result", {"name": "scan", "result": {**sc, "paper": paper}})
            self.memory.mark_quoted(job_id)   # what the paper says stays out of later briefings
            agent.on_event("tool", {"name": "scan", "args": {}, "result": {"pdf": sc["pdf"], "found": sc["found"],
                                                                         "kind": paper["kind"]},
                                    "badge": models.badge(models.SAM)})
            words = instruction or paper["kind"]
            match = self.compiler.match(words, client=agent.client, ladder=agent.ladder, task_id=job_id) if words else None
            upload = handoff.upload_param(match.routine) if match is not None else None
        except (ScanError, handoff.ReadError, ValueError) as e:
            return self._photo_done(agent, job_id, "failed", f"Couldn't scan that photo: {e}.")
        except Exception as e:
            return self._photo_done(agent, job_id, "failed", f"Couldn't scan that photo: {explain(e)}")
        line = handoff.reply(paper, sc) + f", saved as {Path(sc['pdf']).name}"
        if paper["instructions"]:
            line += ". It contained instructions for an AI; I ignored them"
        if match is None or upload is None:
            what = (f"no learned routine for '{words}'" if words else "say what it's for")
            return self._photo_done(agent, job_id, "done", f"{line}. {what[0].upper() + what[1:]}; tell me what to "
                                                           "do with it.")
        if not instruction:   # the paper chose the routine: it may pick nothing else (choices, never free text)
            enums = {p["name"] for p in match.routine.contract.get("params") or [] if p.get("type") == "enum"}
            match.params = {k: v for k, v in match.params.items() if k in enums}
        match.params[upload] = sc["pdf"]
        self.memory.log_step(job_id, "note", line)
        flags = [] if instruction else ["⚠ Chosen from what the paper says, not from your words: approve only if you "
                                        "meant it."]
        try:
            self.compiler.run_task(job_id, words, match, agent=agent, sched=self.scheduler, tainted=True, flags=flags)
        finally:
            if hasattr(agent.toolbox, "_flags"):
                agent.toolbox._flags = []

    def _photo_done(self, agent, job_id: int, status: str, summary: str) -> None:
        self.memory.log_step(job_id, "reply", summary)
        self.memory.finish_task(job_id, status, summary)
        agent.on_event("done", {"id": job_id, "status": status, "summary": summary, "reply": summary,
                                "cost_usd": self.memory.task_usage(job_id)["cost_usd"], "steps": 1})

    # ---- do this Reel (S12, reel.py) ------------------------------------------------------------------------
    def reel(self, video: bytes | None = None, *, path: str = "", words: str = "", transcriber=None) -> int:
        """A how-to video the user shared (bytes from the phone, or a file in a visible zone): steps they review on
        one card, then the normal agent does them. Returns the task."""
        import datetime as _dt
        if self.compiler is None:
            raise ValueError("videos need Lantern's scheduler (lantern --serve)")
        if path:
            p, zone = self.policy.check_visible(path)
            if zone == "private":
                raise ValueError("that video is in a private folder; private files never leave this computer")
            video = self.policy.read_bytes(p)
        if not video:
            raise ValueError("send the video file itself")
        if len(video) > 200 * 1024 * 1024:
            raise ValueError("that video is over 200 MB")
        media = Path(self.cfg.media_dir).expanduser()   # Lantern's own folder: a working copy for ffmpeg
        media.mkdir(parents=True, exist_ok=True)
        saved = media / f"reel-{_dt.datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.mp4"
        saved.write_bytes(video)
        words = " ".join(str(words or "").split())[:200]
        goal = f"Do this video{': ' + words if words else ''}"
        job = self.memory.start_task(goal)
        self.memory.log_step(job.id, "plan", {"goal": goal, "video_bytes": len(video)})
        self.emit({"type": "queued", "task_id": job.id, "goal": goal})
        self.scheduler.submit_job(job.id, goal, lambda agent: self._reel_job(agent, job.id, goal, saved, transcriber))
        return job.id

    def _reel_job(self, agent, job_id: int, goal: str, video: Path, transcriber) -> None:
        from . import reel as reel_mod
        from .reliability import explain
        from .voice import to_wav16k
        agent.toolbox.begin_task(job_id, goal)
        if hasattr(agent.client, "set_task"):
            agent.client.set_task(job_id)
        agent.on_event("task", {"id": job_id, "goal": goal})
        try:
            shots = reel_mod.frames(video)
            transcript, ms = reel_mod.transcript_of(video, transcriber or self._transcriber(), to_wav16k)
            if ms:
                self.memory.log_usage(job_id, models.VOICE, cost_usd=models.audio_cost(ms))
            plan = reel_mod.review(reel_mod.steps_from(agent.client, shots, transcript, memory=self.memory,
                                                       task_id=job_id), self.policy)
        except (reel_mod.ReelError, ValueError) as e:
            return self._photo_done(agent, job_id, "failed", f"Couldn't follow that video: {e}.")
        except Exception as e:
            return self._photo_done(agent, job_id, "failed", f"Couldn't follow that video: {explain(e)}")
        self.memory.mark_quoted(job_id)   # what the video says stays out of later briefings
        self.memory.log_step(job_id, "tool_result", {"name": "reel", "result": plan})
        if plan["refused"]:
            s = next(x for x in plan["steps"] if x["n"] == plan["refused"][0])
            return self._photo_done(agent, job_id, "done", f"I won't do this one: step {s['n']} ({s['do']}) "
                                                           f"{s['why']}. Nothing was done.")
        if plan["unclear"]:   # never guess: ask, and do nothing until the user says
            s = next(x for x in plan["steps"] if x["n"] == plan["unclear"][0])
            return self._photo_done(agent, job_id, "done", f"I couldn't follow step {s['n']} of “{plan['title']}” "
                                                           f"({s['do']}). What exactly does it do? Nothing was done.")
        toolbox = agent.toolbox
        toolbox.tainted = True
        payload = {"title": plan["title"], "steps": [s["do"] for s in plan["steps"]]}
        commit = toolbox._approve("reel_steps", reel_mod.card(plan), payload)
        if commit is None:
            return self._photo_done(agent, job_id, "cancelled", "You said no; nothing from the video was done.")
        if refused := toolbox._commit(commit, payload):
            return self._photo_done(agent, job_id, "failed", f"Not done: {refused.get('reason')}.")
        result = agent.run(reel_mod.goal(plan), task_id=job_id)   # the user's request now: the normal agent
        if getattr(result, "status", None) == "done":
            try:
                offer = self.compiler.offer(job_id)
            except Exception:   # an offer is a nicety; it must never fail a finished task
                offer = None
            if offer is not None:
                self.emit({"type": "learn_offer", "task_id": job_id, "goal": plan["title"], "auto": False})

    def _transcriber(self):
        from .voice import transcribe
        return lambda wav: transcribe(wav)

    # ---- sleep: nightly check and repair (S11, sleep.py) ---------------------------------------------------
    def sleep(self) -> int:
        """Check every learned routine where nothing real can happen, repair what drifted, and send the digest."""
        from . import sleep as sleep_mod
        if self.compiler is None:
            raise ValueError("Sleep needs Lantern's scheduler (lantern --serve)")
        job = self.memory.start_task(sleep_mod.GOAL)
        self.emit({"type": "queued", "task_id": job.id, "goal": sleep_mod.GOAL})
        self.scheduler.submit_job(job.id, sleep_mod.GOAL, lambda agent: self._sleep_job(agent, job.id))
        return job.id

    def _sleep_job(self, agent, job_id: int) -> None:
        from . import sleep as sleep_mod
        from .reliability import explain
        agent.toolbox.begin_task(job_id, sleep_mod.GOAL)
        if hasattr(agent.client, "set_task"):
            agent.client.set_task(job_id)
        agent.on_event("task", {"id": job_id, "goal": sleep_mod.GOAL})
        try:
            report = sleep_mod.run(self.compiler, self.memory, client=agent.client, ladder=agent.ladder,
                                   task_id=job_id)
            status = "done"
        except Exception as e:   # the digest still goes out, saying what stopped it
            report, status = {"checked": [], "digest": f"🌙 Last night's check stopped: {explain(e)}"}, "failed"
        text = report["digest"]
        self.memory.log_step(job_id, "sleep", {k: v for k, v in report.items() if k != "digest"})
        self.memory.log_step(job_id, "reply", text)
        self.memory.finish_task(job_id, status, text)
        self.emit({"type": "digest", "task_id": job_id, "text": text,
                   "healed": sum(1 for x in report["checked"] if x.get("healed")),
                   "broken": sum(1 for x in report["checked"] if x.get("ok") is False)})
        agent.on_event("done", {"id": job_id, "status": status, "summary": text, "reply": text,
                                "cost_usd": self.memory.task_usage(job_id)["cost_usd"], "steps": len(report["checked"])})

    def routines(self) -> list[dict]:
        return self.compiler.list() if self.compiler is not None else []

    def forget_routine(self, name: str) -> bool:
        if self.compiler is None:
            return False
        path = self.compiler.store.path(name)   # KeyError for a name that can't be a routine
        if not path.exists():
            return False
        self.policy.erase_lantern_data(path, Path(self.cfg.db_path).expanduser().parent)   # overwritten, then gone
        self.emit({"type": "routines", "changed": "forgot", "name": name})
        return True

    def _running(self, task_id: int) -> bool:
        return task_id == self.current_task or (self.scheduler is not None and self.scheduler.active(task_id))

    def _work(self) -> None:
        while True:
            task_id, goal, job = self._queue.get()
            if task_id is None:
                return
            self.current_task = task_id
            try:
                job() if job else self.agent.run(goal, task_id=task_id)
            except Exception:
                pass  # the agent already marked the task failed and emitted "done"
            finally:
                self.current_task = None
                self._queue.task_done()

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """For tests and the demo script: wait until the queue is drained."""
        end = time.time() + timeout
        while time.time() < end:
            if self._queue.unfinished_tasks == 0 and not (self.scheduler is not None and self.scheduler.busy()):
                return True
            time.sleep(0.01)
        return False

    def stop(self) -> dict:
        """Kill switch: stop the running task and deny every pending approval."""
        self.agent.cancel.set()
        for session in list(self._teaching.values()):   # a recording in progress is thrown away, never learned
            session["cancelled"] = True
            session["stop"].set()
        stopped = self.scheduler.stop_all() if self.scheduler is not None else []
        denied = self.bus.cancel_all("stopped")
        self.emit({"type": "stopped", "denied_approvals": denied})
        first = self.current_task if self.current_task is not None else (stopped[0] if stopped else None)
        return {"stopped": True, "denied_approvals": denied, "task_id": first, "task_ids": stopped}

    def retry_task(self, task_id: int) -> int:
        """Run a failed or cancelled task's goal again as a new task (an interrupted one is resumed)."""
        t = self.memory.get_task(task_id)
        if t.status in ("active",):
            raise ValueError("that task is still running")
        if t.status == "interrupted":
            return self.resume([task_id])[0]
        if (of := self._undo_of(task_id)) is not None:   # an undo runs again as an undo, never through the model
            return self.undo(of)
        return self.submit(t.goal.split(RESUME_NOTE)[0])

    def resume(self, task_ids: list[int] | None = None) -> list[int]:
        """Queue interrupted tasks again, each told what its cut-short run already did."""
        tasks = ([self.memory.get_task(i) for i in task_ids] if task_ids is not None
                 else self.memory.tasks_with_status("interrupted"))
        new_ids = []
        for t in tasks:
            if t.status != "interrupted":
                continue
            of = self._undo_of(t.id)
            try:
                new_id = self.undo(of) if of is not None else self.submit(resume_goal(self.memory, t))
            except (UndoError, KeyError) as e:   # e.g. the cut-short undo had already put everything back
                self.memory.finish_task(t.id, "failed", f"couldn't resume: {e}")
                continue
            self.memory.finish_task(t.id, "resumed", f"resumed as task #{new_id}")
            self.emit({"type": "resumed", "from": t.id, "task_id": new_id})
            new_ids.append(new_id)
        self.interrupted = [t for t in self.interrupted if t.id not in {x.id for x in tasks}]
        return new_ids

    # ---- undo (no model: replays the task's own verified moves backwards; one approval) -------------
    def undo(self, task_id: int) -> int:
        """Put back what task #task_id moved, as a new task. Raises KeyError, or UndoError with the reason."""
        with self._undo_lock:
            if self._running(task_id) or task_id in self._undoing:
                raise UndoError("that task is still running" if self._running(task_id) else
                                f"an undo of task #{task_id} is already queued")
            plan = undo_plan(self.memory, task_id)
            self._undoing.add(task_id)
        goal = f"Undo task #{task_id}: put back {len(plan)} file{'s' if len(plan) != 1 else ''}"
        task = self.memory.start_task(goal)
        self.memory.log_step(task.id, "plan", {"goal": goal, "undo_of": task_id})
        self._queue.put((task.id, goal, lambda: self._run_undo(task.id, goal, task_id)))
        self.emit({"type": "queued", "task_id": task.id, "goal": goal})
        return task.id

    def undo_last(self) -> int:
        """UNDO from the phone: the most recent finished task that moved files and isn't undone yet."""
        busy = self.current_task
        if busy is None and self.scheduler is not None and self.scheduler.busy():
            busy = (self.scheduler.snapshot()["running"] or [min(self.scheduler.runs, default=0)])[0]
        if busy is not None:   # it may be the one about to move things: don't reach past it
            raise UndoError(f"task #{busy} is still running; undo once it has finished (or say STOP)")
        for t in self.memory.recent_tasks(20):   # undo tasks are skipped, so UNDO again goes one task further back
            if t.status not in ("active", "interrupted") and self._undo_of(t.id) is None:
                try:
                    undo_plan(self.memory, t.id)
                except (UndoError, KeyError):
                    continue
                return self.undo(t.id)
        raise UndoError("none of your recent tasks moved files")

    def _undo_of(self, task_id: int) -> int | None:
        for step in self.memory.steps(task_id)[:1]:
            if step["kind"] == "plan":
                try:
                    of = json.loads(step["content"]).get("undo_of")
                except (TypeError, ValueError, AttributeError):
                    return None
                return of if isinstance(of, int) else None
        return None

    def _run_undo(self, task_id: int, goal: str, of_id: int) -> None:
        try:
            self._undo_job(task_id, goal, of_id)
        finally:   # only now: until the "undone" note is written, a second tap must still be refused
            self._undoing.discard(of_id)

    def _undo_job(self, task_id: int, goal: str, of_id: int) -> None:
        self.agent.cancel.clear()
        self.toolbox.begin_task(task_id, goal)
        self._agent_event("task", {"id": task_id, "goal": goal})
        args = {"of_task": of_id}
        try:
            result = self.toolbox.undo_moves(undo_plan(self.memory, of_id), of_id)   # fresh: files may have moved since
        except (UndoError, ToolError, KeyError) as e:
            result = {"error": str(e).strip("'")}
        except Exception as e:   # never leave the task hanging as "active"
            result = {"error": f"{type(e).__name__}: {e}"}
        self.memory.log_step(task_id, "tool_call", {"name": "undo_moves", "args": args})
        self.memory.log_step(task_id, "tool_result", {"name": "undo_moves", "result": result})
        self._agent_event("tool", {"name": "undo_moves", "args": args, "result": result, "badge": "on this computer"})
        moved, back = result.get("moved", 0), result.get("already_back", 0)
        if result.get("verified"):   # all home; a partial undo stays undoable and the next try finishes it
            self.memory.log_step(of_id, "note", f"{UNDONE}{task_id}")
            self.emit({"type": "undone", "from": of_id, "task_id": task_id, "moved": moved})
        if result.get("error"):
            status, summary = "failed", f"Couldn't undo task #{of_id}: {result['error']}"
        elif result.get("reason") == "user declined":
            status, summary = "cancelled", f"Undo of task #{of_id} declined; nothing moved."
        elif result.get("verified"):
            n = moved + back
            status, summary = "done", f"Put {n} file{'s' if n != 1 else ''} back where they were. Each checked on disk."
        else:
            first = (result.get("failed") or [{}])[0]
            status = "failed"
            summary = (f"Put {moved} of {result.get('of', '?')} back, then stopped: "
                       f"{first.get('error') or result.get('reason') or 'see the steps'}")
        self.memory.log_step(task_id, "reply", summary)
        self.memory.finish_task(task_id, status, summary)
        self._agent_event("done", {"id": task_id, "status": status, "summary": summary, "reply": summary,
                                   "cost_usd": 0.0, "steps": 1})

    def task_view(self, task_id: int) -> dict:
        t = self.memory.get_task(task_id)
        view = {"id": t.id, "goal": t.goal, "status": t.status, "summary": t.summary,
                "created_at": t.created_at, "updated_at": t.updated_at,
                "usage": self.memory.task_usage(t.id), "undoable": undoable(self.memory, t.id)}
        if self.scheduler is not None and (run := self.scheduler.runs.get(t.id)) is not None:
            view["phase"], view["reason"] = run.phase, run.reason   # e.g. parked / waiting_user
        return view

    # ---- forgetting (real deletes: secure_delete + VACUUM; Lantern's own files overwritten) -------
    def forget_task(self, task_id: int) -> dict:
        if self._running(task_id):
            raise ValueError("that task is still running; stop it first")
        self.memory.get_task(task_id)  # KeyError if unknown
        counts = self.memory.forget_task(task_id)
        counts["audit"] = self.audit.forget_task(task_id)
        self.emit({"type": "forgot", "task_id": task_id})
        return {"forgot_task": task_id, **counts}

    def forget_everything(self) -> dict:
        """Memory, audit log, voice notes, screenshots, outbox, Lantern's browser profile and form profile."""
        self.stop()
        self.wait_idle(15)
        result = {"memory": self.memory.forget_everything(), "audit": self.audit.forget_all(), "files": {}, "skipped": []}
        home = Path(self.cfg.db_path).expanduser().parent
        routines = Path(self.cfg.db_path).expanduser().parent / "routines"   # contracts can hold typed values
        for target in (self.cfg.media_dir, self.cfg.browser_profile_dir, self.cfg.profile_path, routines):
            try:
                result["files"][str(target)] = self.policy.erase_lantern_data(target, home)
            except (PolicyError, OSError) as e:
                result["skipped"].append(f"{target}: {e}")
        self.emit({"type": "forgot", "everything": True})
        return result

    # ---- reminders ---------------------------------------------------------------
    def _remind_loop(self) -> None:
        from . import sleep as sleep_mod
        while not self._closing.wait(self.reminder_tick):
            try:
                self.check_reminders()
            except Exception:  # a bad row must never stop the timer
                pass
            try:   # S11: the nightly check, once a night (LANTERN_SLEEP_AT; `lantern --serve` sets 03:30)
                if self.compiler is not None and sleep_mod.due(self.memory):
                    self.sleep()
            except Exception:
                pass

    def check_reminders(self, now: float | None = None) -> list[dict]:
        """Deliver every due reminder (late ones say so); repeating ones move to their next time."""
        from .reminders import LATE_AFTER, next_due
        now = time.time() if now is None else now
        fired = []
        for r in self.memory.due_reminders(now):
            nxt = next_due(r.due_at, r.repeat, now)
            self.memory.reminder_fired(r.id, now, nxt)
            event = {"type": "reminder", "id": r.id, "text": r.text, "repeat": r.repeat,
                     "late": now - r.due_at > LATE_AFTER, "next_due": nxt}
            self.emit(event)
            fired.append(event)
        return fired

    def cancel_reminder(self, reminder_id: int) -> bool:
        ok = self.memory.cancel_reminder(reminder_id)
        if ok:
            self.emit({"type": "reminders", "changed": "cancelled", "id": reminder_id})
        return ok

    def shutdown(self) -> None:
        self._closing.set()
        if self.scheduler is not None:   # scheduled tasks keep their checkpoints and continue on the next start
            self.scheduler.shutdown()
            self.agent.cancel.set()
        else:
            self.stop()
        self.mcp.close()
        self._queue.put((None, None, None))


def _short(result: Any, limit: int = 600) -> Any:
    """Trim big tool results for the event stream (full results stay in memory)."""
    if isinstance(result, dict):
        out = {}
        for k, v in result.items():
            if isinstance(v, str) and len(v) > limit:
                v = v[:limit] + "…"
            out[k] = v
        return out
    return result
