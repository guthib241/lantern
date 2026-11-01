"""Lantern core: wires memory, policy, scrubber/audit, router, approvals and the
agent together, and runs tasks one at a time on a worker thread.

The server (server.py), the channels (WhatsApp/Telegram) and the CLI all talk to
one `Core`. Events from the agent and the approval bus are fanned out to
subscribers (the server's websocket, channels).
"""
from __future__ import annotations

import base64
import json
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import avatar as avatar_mod
from . import config as config_mod
from . import models, packsign
from . import studio as studio_mod
from .agent import Agent
from .agentpacks import MAX_LOOP_FAILURES, Pack, PackError, PackStore, Schedule, draft_pack, pack_suffix, schedule_description, schedule_next
from .approvals import ApprovalBus, ApprovalError, BusGate
from .audit import AuditLog, GuardedClient
from .budget import BudgetGuard, Limits, scheduled_task_budget, usd
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
from .studio import StudioStore
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


def recover_interrupted(memory: Memory) -> list:
    """Tasks still 'active' from a previous run were cut short (crash, restart, power): mark them."""
    out = []
    for t in memory.active_tasks():
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
                 mcp: MCPHub | None = None, reminder_tick: float = 1.0):
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
        self.localsend = None  # the LocalSend Server when Wi-Fi receiving is on (off by default)
        self.envmap = EnvMap(self.policy, cfg.envmap_path)
        self.widgets = Widgets(Path(cfg.widgets_dir))
        self.studio = StudioStore(Path(cfg.db_path).expanduser().parent / "avatar")
        self.packs = PackStore(Path(getattr(cfg, "agents_dir", Path(cfg.db_path).expanduser().parent / "agents")))
        self.mcp = mcp if mcp is not None else MCPHub.from_file(cfg.mcp_config).start()
        self.toolbox = Toolbox(self.policy, self.memory, BusGate(self.bus, approval_timeout), router=self.router,
                               forms=self.forms, email=self.email, envmap=self.envmap, widgets=self.widgets,
                               mcp=self.mcp, audit=self.audit)
        self.toolbox.client = self.client
        self.toolbox.mcp_config = cfg.mcp_config   # learn_repo writes learned servers to Lantern's mcp.json
        if "freecad" in getattr(self.mcp, "servers", {}):
            from .integrations.freecad.connector import FreeCADConnector
            self.toolbox.freecad = FreeCADConnector(self.policy, self.client, self.memory,
                                                    on_event=lambda k, d: self.emit({"type": k, **d}))
        self.checkin = CheckIn(self.client, self.memory, self.emit)
        self.toolbox.on_event = self._agent_event
        self.agent = Agent(self.client, self.memory, self.toolbox, model=cfg.model, persona=cfg.persona,
                           on_event=self._agent_event)
        self._sinks: list[EventSink] = []
        self._queue: queue.Queue = queue.Queue()
        self._loops_lock = threading.Lock()   # loop state lives in the pack files; the reminder thread and routes share it
        self._undoing: set[int] = set()   # tasks with an undo queued (a double tap queues one)
        self._undo_lock = threading.Lock()
        self.interrupted = recover_interrupted(self.memory)   # before the worker starts taking tasks
        self.bus.subscribe(self._approval_event)
        self._worker = threading.Thread(target=self._work, name="lantern-agent", daemon=True)
        self._worker.start()
        self._closing = threading.Event()
        self.reminder_tick = reminder_tick
        if reminder_tick > 0:
            threading.Thread(target=self._remind_loop, name="lantern-reminders", daemon=True).start()

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
        if kind in ("decided", "expired", "used") and self.current_task is not None:   # for the task's receipt
            self.memory.log_step(self.current_task, "approval", {
                "id": approval.id, "event": kind, "action": approval.action, "summary": approval.summary,
                "payload_hash": approval.payload_hash, "status": approval.status, "by": approval.decided_by,
                "tainted": approval.tainted})
        self.emit({"type": "approval", "event": kind, "approval": approval.public(include_nonce=(kind == "requested")),
                   "task_id": self.current_task})

    # ---- tasks -----------------------------------------------------------------
    def submit(self, goal: str, pack: str | None = None, inputs: dict | None = None) -> int:
        if pack is not None:   # the pack renders the goal from its blanks; `goal` is unused
            return self.submit_pack(pack, inputs or {})
        goal = goal.strip()
        if not goal:
            raise ValueError("goal is empty")
        task = self.memory.start_task(goal)
        self._queue.put((task.id, goal, None))
        self.emit({"type": "queued", "task_id": task.id, "goal": goal, "pack": None})
        return task.id

    def submit_pack(self, name: str, inputs: dict, task_budget: float | None = None) -> int:
        """Run an installed agent pack: the goal is rendered from its blanks,
        its instructions run last, its tools are allowlisted and the task starts
        tainted. The pack is re-validated on every run, so a file edited on disk
        afterwards is refused. Stopping uses the kill switch; watching uses the
        task timeline. `task_budget` caps one run's spending (scheduled loops)."""
        try:
            found = self.packs.get(name)   # re-verifies the signature, not only at install
        except KeyError:
            raise KeyError(name) from None
        except PackError as e:
            raise ValueError(str(e)) from None
        if found.disabled:
            raise ValueError(f"agent {name!r} is disabled")
        if found.needs:
            outcome = self.ensure_pack_servers(found)
            missing = list(outcome["declined"]) + sorted(outcome["failed"])
            if missing:
                raise ValueError(f"agent {name!r} needs {', '.join(missing)} "
                                 f"({'; '.join(outcome['failed'].values()) or 'user declined'}); "
                                 "nothing was started")
        try:
            rendered = found.render_goal(inputs)
        except PackError as e:
            raise ValueError(str(e)) from None
        return self._queue_pack_task(found, rendered, task_budget=task_budget)

    def _queue_pack_task(self, pack: Pack, rendered: str, task_budget: float | None = None) -> int:
        """Queue a validated pack's run as an ordinary task (timeline, receipt, Stop)."""
        goal = f"[agent:{pack.name}] {rendered}"
        suffix, allowed = pack_suffix(pack), set(pack.tools)
        task = self.memory.start_task(goal)
        if task_budget is not None:
            self.budget.set_task_budget(task.id, task_budget)
        self._queue.put((task.id, goal,
                         lambda: self.agent.run(goal, task_id=task.id, instructions_suffix=suffix,
                                                allowed_tools=allowed, taint=True)))
        self.emit({"type": "queued", "task_id": task.id, "goal": goal, "pack": pack.name})
        return task.id

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
            if task_id == self.current_task or task_id in self._undoing:
                raise UndoError("that task is still running" if task_id == self.current_task else
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
        if self.current_task is not None:   # it may be the one about to move things: don't reach past it
            raise UndoError(f"task #{self.current_task} is still running; undo once it has finished (or say STOP)")
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
        return {"id": t.id, "goal": t.goal, "status": t.status, "summary": t.summary,
                "created_at": t.created_at, "updated_at": t.updated_at,
                "usage": self.memory.task_usage(t.id), "undoable": undoable(self.memory, t.id)}

    # ---- agent packs (shareable agents; T3a) --------------------------------------
    def pack_install(self, data: dict, *, replace: bool = False) -> dict:
        """Install a pack after the user approves the exact file (bound to its SHA-256).

        Trust on first use: a signed pack from a new key asks "Trust it?" and
        approving records the key; the same author name on a different key warns."""
        import hashlib
        from .agentpacks import canonical_bytes, validate
        data = dict(data)
        source = data.pop("source", None)   # local-only: where it came from, never in the shared file
        if source != "imported skill":
            source = None
        skipped = data.pop("skipped", None)   # local-only too: scripts the import listed, never copied
        if not (isinstance(skipped, list) and all(isinstance(s, str) for s in skipped)):
            skipped = []
        pack = validate(data)   # PackError if invalid or tampered: no approval asked for a bad pack
        raw = canonical_bytes(pack.as_dict())
        payload = {"name": pack.name, "sha256": hashlib.sha256(raw).hexdigest()}
        summary = (f"Install agent '{pack.title}' by {pack.author} (v{pack.version}). "
                   f"It may use: {', '.join(pack.tools)}. "
                   f"Full instructions: {pack.instructions}")
        if source == "imported skill":
            summary += " Imported from a community skill (SKILL.md)."
        if skipped:
            summary += f" Not imported (packs never run code): {', '.join(skipped)}."
        summary += self._signature_line(pack)
        commit = self.toolbox.gate.ask("install_agent", summary, payload, tainted=False)
        if commit is None:
            return {"installed": False, "reason": "user declined", "name": pack.name}
        try:
            commit(payload)
        except ApprovalError as e:
            return {"installed": False, "reason": str(e), "name": pack.name}
        try:
            stored = self.packs.install(pack.as_dict(), replace=replace, source=source)
        except PackError as e:
            return {"installed": False, "reason": str(e), "name": pack.name}
        if pack.signature is not None:   # approving a new key records it; declining records nothing
            fp = packsign.fingerprint(pack.signature["public_key"])
            packsign.record_trust(self.packs.dir, fp, pack.author)
        result: dict = {"installed": True, "verified": stored.as_dict() == pack.as_dict(), "name": pack.name}
        if pack.needs:
            result["needed_servers"] = self.ensure_pack_servers(pack)
        return result

    # ---- packs that need MCP servers (T8) -----------------------------------------
    def ensure_pack_servers(self, pack: Pack) -> dict:
        """Add each MCP server the pack declares in `needs` that isn't in
        mcp.json yet. Each missing server asks first ("This agent needs the
        GitHub tool server: Add it?"); declining adds nothing. Reuses the T6
        flow: the repo is read for its pinned commit and license, the red card
        shows both plus the exact launch command, and the entry is stored with
        an empty `read_only` (every tool call still needs approval)."""
        from . import repolearn
        added: list[str] = []
        declined: list[str] = []
        failed: dict[str, str] = {}
        if not pack.needs:
            return {"added": added, "declined": declined, "failed": failed}
        path = Path(self.toolbox.mcp_config).expanduser() if self.toolbox.mcp_config else None
        servers: dict = {}
        if path is not None and path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("servers"), dict):
                    servers = data["servers"]
            except ValueError:
                pass
        for need in pack.needs:
            if need.mcp in servers:
                continue
            if need.mcp != repolearn.GITHUB_SERVER or need.repo != repolearn.GITHUB_MCP_REPO:
                failed[need.mcp] = (f"Lantern doesn't know how to add the '{need.mcp}' server "
                                    f"({need.repo}) yet; add it to mcp.json by hand")
                continue
            self._add_github_server(pack, need, servers, added, declined, failed)
        return {"added": added, "declined": declined, "failed": failed}

    def _add_github_server(self, pack: Pack, need: Any, servers: dict,
                           added: list[str], declined: list[str], failed: dict[str, str]) -> None:
        """One missing GitHub tool server: read the repo (pinned SHA, license),
        ask on the red card, and write the mcp.json entry on yes."""
        from . import repolearn
        if not self.toolbox.mcp_config:
            failed[need.mcp] = "mcp.json isn't set up on this computer"
            return
        owner, repo_name = need.repo.split("/", 1)
        try:
            repo = repolearn.fetch_repo(owner, repo_name, None, get=self.toolbox.github_get)
        except repolearn.RepoError as e:
            failed[need.mcp] = str(e)   # no approval asked for a repo we can't read
            return
        spec = repolearn.github_server_spec(repo.sha)
        if repo.sha not in json.dumps(spec):
            failed[need.mcp] = "the install plan doesn't pin the commit"
            return
        card = repolearn.github_install_card(repo, spec) + f"\nFor: {need.why}"
        payload = {"pack": pack.name, "server": need.mcp, "repo": need.repo,
                   "sha": repo.sha, "spec": spec}
        commit = self.toolbox.gate.ask("add_needed_server", card, payload, tainted=False)
        if commit is None:
            declined.append(need.mcp)
            return
        try:
            commit(payload)
        except ApprovalError as e:
            failed[need.mcp] = str(e)
            return
        try:
            repolearn.add_learned_server(self.toolbox.mcp_config, need.mcp, spec)
        except (OSError, ValueError) as e:
            failed[need.mcp] = f"couldn't write mcp.json ({e})"
            return
        try:   # verify: read it back (the running hub picks it up on restart)
            back = json.loads(Path(self.toolbox.mcp_config).expanduser().read_text(encoding="utf-8"))
            stored = back.get("servers", {}).get(need.mcp, {})
        except (OSError, ValueError):
            stored = {}
        if stored.get("source", {}).get("sha") == repo.sha and stored.get("read_only") == []:
            servers[need.mcp] = spec
            added.append(need.mcp)
        else:
            failed[need.mcp] = "the server didn't store correctly"

    def _signature_line(self, pack) -> str:
        """What the install card says about who sealed the pack (packsign; T3e)."""
        if pack.signature is None:
            return " Unsigned: anyone could have changed it."
        fp = packsign.fingerprint(pack.signature["public_key"])
        trusted = packsign.load_trusted(self.packs.dir)
        if fp in trusted:
            return f" ✓ Signed by {pack.author} · key {fp}."
        line = f" New author key {fp} ({pack.author}). Trust it?"
        if any(name == pack.author for name in trusted.values()):
            line += f" ⚠️ {pack.author}'s packs were signed with a different key before."
        return line

    def pack_author_is_you(self, author: str) -> bool:
        """Is this pack's author you: LANTERN_AUTHOR, or the profile's full name."""
        from .forms import Vault
        yours = [os.environ.get("LANTERN_AUTHOR", "")]
        try:
            profile = Vault(self.cfg.profile_path).load().get("profile", {})
            yours += [profile.get("full name", ""), profile.get("name", "")]
        except (OSError, ValueError):
            pass
        mine = {str(v).strip().casefold() for v in yours if str(v).strip()}
        return bool(mine) and str(author or "").strip().casefold() in mine

    def pack_sign(self, name: str) -> dict:
        """Sign an installed pack with this laptop's key, after an approval.
        Replaces any previous signature; the disabled flag and face stay as they are."""
        import hashlib
        from .agentpacks import canonical_bytes
        try:
            current = self.packs.get(name)   # re-validated: a changed file is refused, not signed
        except KeyError:
            raise KeyError(name) from None
        unsigned = {k: v for k, v in current.as_dict().items() if k != "signature"}
        home = Path(self.cfg.db_path).expanduser().parent
        private_key, public_b64 = packsign.ensure_key(home)
        fp = packsign.fingerprint(public_b64)
        payload = {"name": name, "sha256": hashlib.sha256(canonical_bytes(unsigned)).hexdigest(),
                   "fingerprint": fp}
        summary = (f"Sign agent '{current.title}' by {current.author} with your key {fp}? "
                   "Anyone you share it with will see it is from you, and whether it changed.")
        commit = self.toolbox.gate.ask("sign_agent", summary, payload, tainted=False)
        if commit is None:
            return {"signed": False, "reason": "user declined", "name": name}
        try:
            commit(payload)
        except ApprovalError as e:
            return {"signed": False, "reason": str(e), "name": name}
        signed = packsign.sign(unsigned, private_key)
        try:
            stored = self.packs.store_signed(name, signed)
        except PackError as e:
            return {"signed": False, "reason": str(e), "name": name}
        ok = stored.as_dict() == signed   # verify: read it back
        return {"signed": True, "verified": ok, "name": name, "fingerprint": fp}

    # ---- avatar studio (Lantern's face, designed in words; T4a) ------------------------
    def design_avatar(self, brief: str, style: str) -> dict:
        """Draw one canonical preview image from the brief (about $0.01). No approval:
        the preview is cheap and the accept step carries the approval for the sheet."""
        clean = studio_mod.validate_brief(brief)   # StudioError before any model call
        key = studio_mod.validate_style(style)
        design_id = studio_mod.new_id()
        folder = self.studio.design_dir(design_id)
        sheet = avatar_mod.generate(self.client, folder, brief=studio_mod.design_brief(clean, key), only=["idle"],
                                    on_cost=lambda usd: self.memory.log_usage(None, models.IMAGE, cost_usd=usd))
        preview = (folder / "idle.webp").read_bytes()
        self.studio.save_meta(design_id, studio_mod.new_meta(design_id, clean, key,
                                                             sheet.canonical_response, sheet.cost_usd))
        return {"id": design_id, "brief": clean, "style": key, "cost_usd": sheet.cost_usd,
                "preview": "data:image/webp;base64," + base64.b64encode(preview).decode()}

    def accept_avatar(self, design_id: str) -> dict:
        """Render the remaining expressions anchored on the preview (after approval),
        write the sheet to ~/.lantern/avatar/<id>/ and set it active. A finished
        design just becomes active again: no model call, no approval."""
        try:
            meta = self.studio.get_meta(design_id)
        except studio_mod.StudioError:
            raise KeyError(design_id) from None
        folder = self.studio.design_dir(design_id)
        rest = [e for e in avatar_mod.EXPRESSIONS if not (folder / f"{e}.webp").exists()
                and not (folder / f"{e}.png").exists()]
        if not rest:   # the gallery switch-back: already on disk, nothing to draw
            self.studio.set_active(design_id)
            self.emit({"type": "avatar", "active": design_id})
            return {"accepted": True, "verified": True, "id": design_id, "cost_usd": 0.0, "model_calls": 0}
        payload = {"id": design_id, "images": len(rest), "cost_usd": round(models.image_cost(len(rest)), 4)}
        summary = studio_mod.finish_summary(meta.get("style", "?"), len(rest))
        commit = self.toolbox.gate.ask("finish_avatar", summary, payload)
        if commit is None:
            return {"accepted": False, "reason": "user declined", "id": design_id}
        try:
            commit(payload)
        except ApprovalError as e:
            return {"accepted": False, "reason": str(e), "id": design_id}
        # The preview's manifest holds the canonical turn; generate() chains the rest on it.
        sheet = avatar_mod.generate(self.client, folder, only=rest,
                                    on_cost=lambda usd: self.memory.log_usage(None, models.IMAGE, cost_usd=usd))
        self.studio.save_meta(design_id, {**meta, "status": "complete",
                                          "cost_usd": round(meta.get("cost_usd", 0.0) + sheet.cost_usd, 4)})
        self.studio.set_active(design_id)
        self.emit({"type": "avatar", "active": design_id})
        verified = all((folder / f"{e}.webp").exists() or (folder / f"{e}.png").exists()
                       for e in avatar_mod.EXPRESSIONS)
        return {"accepted": True, "verified": verified, "id": design_id,
                "cost_usd": sheet.cost_usd, "model_calls": len(rest)}

    def make_agent_face(self, name: str, *, emoji: str | None = None, generate: bool = False) -> dict:
        """Give an agent a face for the avatar window: a short emoji (free, kept at
        once) or one drawn Muse Image icon (about $0.01, behind an approval).
        The icon is a separate file; the shared pack file stays data-only."""
        from .agentpacks import PackError as _PackError
        from .agentpacks import face_prompt, validate_emoji
        try:
            pack = self.packs.get(name)
        except KeyError:
            raise KeyError(name) from None
        if emoji is not None and generate:
            raise ValueError("give an emoji or ask to draw, not both")
        if emoji is not None:
            try:
                face = self.packs.set_face_emoji(name, validate_emoji(emoji))
            except _PackError as e:
                raise ValueError(str(e)) from None
            return {"made": True, "face": face, "cost_usd": 0.0, "model_calls": 0}
        if not generate:
            raise ValueError("say what the face is: an emoji, or generate=true to draw one")
        payload = {"name": name, "images": 1, "cost_usd": round(models.image_cost(1), 4)}
        summary = (f"Draw a face for agent '{pack.title}' (one Muse Image icon, "
                   f"about ${models.image_cost(1):.2f}).")
        commit = self.toolbox.gate.ask("agent_face", summary, payload)
        if commit is None:
            return {"made": False, "reason": "user declined", "name": name}
        try:
            commit(payload)
        except ApprovalError as e:
            return {"made": False, "reason": str(e), "name": name}
        image, _ = avatar_mod.render_single(self.client, face_prompt(pack))
        face = self.packs.save_face_icon(name, image)
        cost = models.image_cost(1)
        self.memory.log_usage(None, models.IMAGE, cost_usd=cost)
        return {"made": True, "verified": True, "face": face, "cost_usd": cost, "model_calls": 1}

    def pack_import_skill(self, body: dict) -> dict:
        """Preview a SKILL.md import: parse it into a validated pack plus the
        scripts that were listed but never copied. Saving it goes through the
        normal install (with its approval), where the panel's tool checkboxes
        land. Imports read only through the privacy policy. No approval: a
        preview spends nothing, and an imported pack runs tainted like any pack."""
        from .skillimport import SkillImportError, import_path, import_text
        if not isinstance(body, dict):
            raise SkillImportError("send a SKILL.md path or its text")
        try:
            if body.get("path"):
                return import_path(self.policy, body["path"])
            if body.get("text"):
                return import_text(body["text"], filename=body.get("filename") or "SKILL.md")
        except SkillImportError:
            raise
        except (ValueError, OSError) as e:
            raise SkillImportError(str(e)) from None
        raise SkillImportError("send a SKILL.md path or its text")

    def pack_a2a_card(self, name: str) -> dict:
        """This installed pack as an A2A Agent Card (no endpoint URL: Lantern
        doesn't serve agents to the network). Raises KeyError if unknown."""
        from .a2acard import card_for_pack
        try:
            pack = self.packs.get(name)   # re-validated: a changed file is refused, not exported
        except KeyError:
            raise KeyError(name) from None
        return card_for_pack(pack)

    def pack_draft(self, task_id: int) -> dict:
        """Draft a pack from a finished task (Muse Spark, structured output).
        The draft is validated; saving it goes through install (with approval)."""
        from .agentpacks import PackError as _PackError
        try:
            data, _ = draft_pack(self.client, self.memory, task_id)
        except KeyError:
            raise ValueError(f"no task #{task_id}") from None
        except _PackError as e:
            raise ValueError(str(e)) from None
        return data

    # ---- agent surfaces (where a pack may start: laptop, phone, or both; T3d) --
    def pack_surface(self, name: str) -> str:
        """This pack's Runs-on setting (kept in the store, never in the shared file)."""
        try:
            return self.packs.surface(name)
        except KeyError:
            raise KeyError(name) from None

    def set_pack_surface(self, name: str, surface: str) -> dict:
        """Keep a new Runs-on setting. No approval: it spends nothing. Raises
        KeyError if unknown, ValueError if the surface is not one of the three."""
        from .agentpacks import PackError as _PackError
        from .agentpacks import validate_surface
        try:
            validate_surface(surface)
        except _PackError as e:
            raise ValueError(str(e)) from None
        try:
            saved = self.packs.set_surface(name, surface)
        except KeyError:
            raise KeyError(name) from None
        self.emit({"type": "agents", "event": "surface", "name": name, "surface": saved})
        return {"name": name, "surface": saved}

    # ---- loop agents (packs on a schedule; T3c) -----------------------------------
    def scheduled_budget(self) -> float | None:
        """One scheduled run's own task budget: LANTERN_TASK_BUDGET_USD, default $0.10."""
        return scheduled_task_budget(dict(os.environ))

    def loops(self) -> list[dict]:
        """Loop status for every installed pack that suggests a schedule."""
        return [self._loop_view(e["name"], e) for e in self.packs.list() if e.get("schedule") is not None]

    def loop_status(self, name: str) -> dict:
        """Loop status for one pack (scheduled or not). Raises KeyError if unknown."""
        pack = self.packs.get(name)   # KeyError if unknown; PackError if tampered
        return self._loop_view(name, {**pack.as_dict(), "loop": self.packs.loop_state(name)})

    @staticmethod
    def _loop_view(name: str, entry: dict) -> dict:
        sched = entry.get("schedule")
        words = None
        if isinstance(sched, Schedule):
            words, sched = schedule_description(sched), sched.as_dict()
        elif isinstance(sched, dict):
            try:
                words = schedule_description(Schedule(**{k: sched.get(k)
                                                         for k in ("every", "at", "hours", "inputs")}))
            except (TypeError, KeyError):
                words = None
        loop = dict(entry.get("loop") or {})
        return {"name": name, "title": entry.get("title", name), "scheduled": sched is not None,
                "schedule": sched, "description": words,
                "enabled": bool(loop.get("enabled", False)), "next_run": loop.get("next_run"),
                "last_task": loop.get("last_task"), "last_status": loop.get("last_status"),
                "failures": loop.get("failures", 0), "running": loop.get("running")}

    def loop_resume(self, name: str) -> dict:
        """Turn a loop on, after an approval card showing how often it runs and the
        per-run budget. A run still needs its own approvals; unanswered cards count
        as declined. The loop starts from now, never by catching up."""
        try:
            pack = self.packs.get(name)   # re-validated: a changed file is refused, not looped
        except KeyError:
            raise KeyError(name) from None
        except PackError as e:
            raise ValueError(str(e)) from None
        if pack.schedule is None:
            raise ValueError(f"agent {name!r} has no schedule")
        if pack.disabled:
            raise ValueError(f"agent {name!r} is disabled")
        budget = self.scheduled_budget()
        per_run = usd(budget) if budget is not None else "no per-run cap"
        summary = (f"Run agent '{pack.title}' {schedule_description(pack.schedule)} (about {per_run} per run). "
                   f"Each run is an ordinary task with its own timeline and receipt; every change still needs "
                   f"approval, unanswered cards count as declined, and it pauses itself after "
                   f"{MAX_LOOP_FAILURES} failed runs in a row.")
        payload = {"name": name, "schedule": pack.schedule.as_dict(), "budget_usd": budget}
        commit = self.toolbox.gate.ask("enable_loop", summary, payload, tainted=False)
        if commit is None:
            return {"enabled": False, "reason": "user declined", "name": name}
        try:
            commit(payload)
        except ApprovalError as e:
            return {"enabled": False, "reason": str(e), "name": name}
        loop = self.packs.loop_state(name)
        loop.update(enabled=True, failures=0, next_run=schedule_next(pack.schedule, time.time()))
        saved = self.packs.save_loop(name, loop)
        self.emit({"type": "loop", "event": "resumed", "name": name, "next_run": saved["next_run"]})
        return {"enabled": True, "name": name, "next_run": saved["next_run"], "budget_usd": budget}

    def loop_pause(self, name: str) -> dict:
        """Turn a loop off. A run already going keeps going (STOP stops it).
        No approval: pausing spends nothing."""
        try:
            loop = self.packs.loop_state(name)
        except KeyError:
            raise KeyError(name) from None
        loop["enabled"] = False
        saved = self.packs.save_loop(name, loop)
        self.emit({"type": "loop", "event": "paused", "name": name, "failures": saved["failures"],
                   "text": f"Paused the '{name}' loop."})
        return {"name": name, "enabled": False}

    def loop_run_now(self, name: str) -> int:
        """Start this loop's scheduled run right away, as an ordinary task."""
        try:
            pack = self.packs.get(name)
        except KeyError:
            raise KeyError(name) from None
        except PackError as e:
            raise ValueError(str(e)) from None
        if pack.schedule is None:
            raise ValueError(f"agent {name!r} has no schedule")
        if pack.disabled:
            raise ValueError(f"agent {name!r} is disabled")
        try:
            rendered = pack.render_goal(pack.schedule.inputs)
        except PackError as e:
            raise ValueError(str(e)) from None
        return self._queue_pack_task(pack, rendered)

    def check_loops(self, now: float | None = None) -> list[dict]:
        """Settle finished loop runs and fire every due loop (each run is an ordinary
        pack task: allowlisted, tainted, budgeted). Called from the reminder loop;
        tests pass a fake clock. One bad pack never stops the other loops."""
        now = time.time() if now is None else now
        fired = []
        with self._loops_lock:
            for entry in self.packs.list():
                try:
                    self._check_loop(entry, now, fired)
                except Exception:
                    pass
        return fired

    def _check_loop(self, entry: dict, now: float, fired: list) -> None:
        name = entry["name"]
        try:
            pack = self.packs.get(name)   # re-validated: an edited file refuses instead of running
        except (KeyError, PackError, ValueError, OSError):
            return
        loop = dict(entry.get("loop") or self.packs.loop_state(name))
        if loop.get("running") is not None and not self._loop_running(loop["running"]):
            loop = self._settle_loop(pack, loop)
            if loop is None:   # just paused itself after repeated failures: stay quiet this tick
                return
        if not loop["enabled"] or pack.disabled or pack.schedule is None:
            return
        if loop["running"] is not None:
            return   # the previous run is still going: never overlap
        if loop["next_run"] is None or now < loop["next_run"]:
            return
        try:
            rendered = pack.render_goal(pack.schedule.inputs)
        except PackError:
            return
        try:
            task_id = self._queue_pack_task(pack, rendered, task_budget=self.scheduled_budget())
        except Exception:
            loop["next_run"] = schedule_next(pack.schedule, now, last=now)   # try again next time, not every tick
            self.packs.save_loop(name, loop)
            return
        loop["running"] = task_id
        loop["next_run"] = schedule_next(pack.schedule, now, last=now)
        self.packs.save_loop(name, loop)
        event = {"type": "loop", "event": "fired", "name": name, "task_id": task_id}
        self.emit(event)
        fired.append(event)

    def _loop_running(self, task_id: int) -> bool:
        try:
            return self.memory.get_task(task_id).status == "active"
        except KeyError:
            return False   # forgotten while running: treat it as settled

    def _settle_loop(self, pack, loop: dict) -> dict | None:
        try:
            status = self.memory.get_task(loop["running"]).status
        except KeyError:
            status = "failed"
        self.budget.clear_task_budget(loop["running"])
        loop["last_task"], loop["last_status"], loop["running"] = loop["running"], status, None
        loop["failures"] = 0 if status == "done" else loop["failures"] + 1
        if loop["failures"] >= MAX_LOOP_FAILURES and loop["enabled"]:
            loop["enabled"] = False
            self.packs.save_loop(pack.name, loop)
            self.emit({"type": "loop", "event": "paused", "name": pack.name, "failures": loop["failures"],
                       "text": (f"Paused agent '{pack.title}': {loop['failures']} failed runs in a row. "
                                "Resume it in the Agents tab.")})
            return None
        self.packs.save_loop(pack.name, loop)
        return loop

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

    # ---- reminders ---------------------------------------------------------------
    def _remind_loop(self) -> None:
        while not self._closing.wait(self.reminder_tick):
            try:
                self.check_reminders()
            except Exception:  # a bad row must never stop the timer
                pass

    def check_reminders(self, now: float | None = None) -> list[dict]:
        """Deliver every due reminder (late ones say so); repeating ones move to their next time.
        Loop agents ride the same timer (no second thread): due loops fire here too."""
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
        self.check_loops(now)
        return fired

    def cancel_reminder(self, reminder_id: int) -> bool:
        ok = self.memory.cancel_reminder(reminder_id)
        if ok:
            self.emit({"type": "reminders", "changed": "cancelled", "id": reminder_id})
        return ok

    def shutdown(self) -> None:
        self._closing.set()
        self.stop()
        try:
            self.stop_localsend()
        except Exception:
            pass
        self.mcp.close()
        self._queue.put((None, None, None))

    # ---- LocalSend receiver (T5): files and links from phones over home Wi-Fi ----
    def start_localsend(self) -> dict:
        """Start the LocalSend receiver. Raises RuntimeError if the inbox is
        not in a writable zone (refuses to start instead of saving elsewhere)."""
        from . import localsend as localsend_mod
        if self.localsend is not None and self.localsend.listening:
            return self.localsend.status()
        receiver = localsend_mod.Receiver(
            self.policy, BusGate(self.bus), audit=self.audit,
            on_link=lambda url: self.submit(f"[localsend] open this link on my laptop: {url}"),
            emit=self.emit, home=Path(self.cfg.db_path).expanduser().parent,
            inbox=localsend_mod.inbox_in(dict(os.environ)),
            pin=localsend_mod.pin_in(dict(os.environ)))
        server = localsend_mod.Server(receiver, port=localsend_mod.port_in(dict(os.environ)))
        server.start()  # check_ready raises before anything listens
        self.localsend = server
        self.emit({"type": "localsend", "listening": True, "port": server.port})
        return server.status()

    def stop_localsend(self) -> dict:
        if self.localsend is not None:
            try:
                self.localsend.stop()
            finally:
                self.localsend = None
            self.emit({"type": "localsend", "listening": False})
        return {"listening": False}


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
