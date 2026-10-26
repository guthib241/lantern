"""Lantern core: wires memory, policy, scrubber/audit, router, approvals and the
agent together, and runs tasks one at a time on a worker thread.

The server (server.py), the channels (WhatsApp/Telegram) and the CLI all talk to
one `Core`. Events from the agent and the approval bus are fanned out to
subscribers (the server's websocket, channels).
"""
from __future__ import annotations

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
from .crypto import cipher_for
from .email_tool import EmailService, EmailSettings
from .envmap import EnvMap
from .forms import FormService, FormSettings
from .mcp_client import MCPHub
from .memory import Memory
from .policy import Policy, PolicyError
from .router import Router, RouterConfig, make_backend
from .sam import CheckIn
from .tools import Toolbox
from .widgets import Widgets

EventSink = Callable[[dict], None]


class NoModelClient:
    """Stands in when MODEL_API_KEY is missing, so the server and UI still start."""

    class _R:
        def create(self, **kw):
            raise RuntimeError(f"set {models.API_KEY_ENV} to run tasks (or start with --demo)")

    def __init__(self):
        self.responses = self._R()


class Core:
    def __init__(self, cfg: config_mod.Config, client: Any = None, *, memory: Memory | None = None,
                 audit: AuditLog | None = None, policy: Policy | None = None, router: Router | None = None,
                 bus: ApprovalBus | None = None, approval_timeout: float | None = None,
                 forms: FormService | None = None, email: EmailService | None = None,
                 mcp: MCPHub | None = None):
        self.cfg = cfg
        cipher = None if (memory and audit) else cipher_for(cfg)
        self.memory = memory or Memory(cfg.db_path, cipher)
        self.audit = audit or AuditLog(cfg.audit_path, cipher)
        self.policy = policy or Policy.load(cfg.zones_file, cfg.roots)
        self.router = router or Router(self.policy, make_backend(RouterConfig.from_env(dict(os.environ)), self.audit))
        self.bus = bus or ApprovalBus()
        if client is None:
            client = self._default_client()
        self.client = client if isinstance(client, GuardedClient) else GuardedClient(client, self.audit,
                                                                                     destination=cfg.base_url)
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
        self.current_task: int | None = None
        self.bus.subscribe(self._approval_event)
        self._worker = threading.Thread(target=self._work, name="lantern-agent", daemon=True)
        self._worker.start()

    def _default_client(self):
        key = os.environ.get(models.API_KEY_ENV)
        if not key:
            return NoModelClient()
        from openai import OpenAI
        return OpenAI(base_url=self.cfg.base_url, api_key=key, timeout=120, max_retries=2)

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
        self.emit({"type": "approval", "event": kind, "approval": approval.public(include_nonce=(kind == "requested")),
                   "task_id": self.current_task})

    # ---- tasks -----------------------------------------------------------------
    def submit(self, goal: str) -> int:
        goal = goal.strip()
        if not goal:
            raise ValueError("goal is empty")
        task = self.memory.start_task(goal)
        self._queue.put((task.id, goal))
        self.emit({"type": "queued", "task_id": task.id, "goal": goal})
        return task.id

    def _work(self) -> None:
        while True:
            task_id, goal = self._queue.get()
            if task_id is None:
                return
            self.current_task = task_id
            try:
                self.agent.run(goal, task_id=task_id)
            except Exception:
                pass  # the agent already marked the task failed and emitted "done"
            finally:
                self.current_task = None
                self._queue.task_done()

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """For tests and the demo script: wait until the queue is drained."""
        end = time.time() + timeout
        while time.time() < end:
            if self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.01)
        return False

    def stop(self) -> dict:
        """Kill switch: stop the running task and deny every pending approval."""
        self.agent.cancel.set()
        denied = self.bus.cancel_all("stopped")
        self.emit({"type": "stopped", "denied_approvals": denied})
        return {"stopped": True, "denied_approvals": denied, "task_id": self.current_task}

    def task_view(self, task_id: int) -> dict:
        t = self.memory.get_task(task_id)
        return {"id": t.id, "goal": t.goal, "status": t.status, "summary": t.summary,
                "created_at": t.created_at, "updated_at": t.updated_at,
                "usage": self.memory.task_usage(t.id)}

    # ---- forgetting (real deletes: secure_delete + VACUUM; Lantern's own files overwritten) -------
    def forget_task(self, task_id: int) -> dict:
        if task_id == self.current_task:
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
        for target in (self.cfg.media_dir, self.cfg.browser_profile_dir, self.cfg.profile_path):
            try:
                result["files"][str(target)] = self.policy.erase_lantern_data(target, home)
            except (PolicyError, OSError) as e:
                result["skipped"].append(f"{target}: {e}")
        self.emit({"type": "forgot", "everything": True})
        return result

    def shutdown(self) -> None:
        self.stop()
        self.mcp.close()
        self._queue.put((None, None))


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
