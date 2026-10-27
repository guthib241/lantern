"""Lantern's local API: FastAPI on 127.0.0.1 only.

Every route except /health needs the per-install token from ~/.lantern/token
(header `X-Lantern-Token`, or `?token=` for the websocket), so a random web page
in the browser can't drive Lantern through localhost.

  GET  /health                      liveness, model, whether a key is set
  POST /task         {goal}         queue a task -> {task_id}
  GET  /tasks                       recent tasks with status, summary, cost
  GET  /tasks/{id}                  one task with its step timeline
  GET  /tasks/{id}/receipt          what it changed and checked, what you approved, what left, what it cost
  GET  /approvals                   pending approvals (with nonce, for the approval card)
  POST /approvals/{id} {nonce, approve, expected_hash?}
  POST /stop                        kill switch
  GET  /audit                       what left the laptop today + recent entries
  GET  /zones, POST /zones {path, zone}
  GET  /memory                      remembered facts
  GET  /usage                       tokens and cost per model, and the spending limits (budget.py)
  GET  /profile, POST /profile {key, value, section?}   the form profile vault (M7)
  GET  /webhook/whatsapp            WhatsApp handshake (no token: hub.verify_token instead)
  POST /webhook/whatsapp            WhatsApp messages (no token: X-Hub-Signature-256 instead)
  GET  /widgets, GET /widgets/{name}   sandboxed widgets for the desktop (re-checked on every load)
  POST /forget/task/{id}, POST /forget/fact {key}, POST /forget/everything {confirm: "forget everything"}
  POST /checkin {image: data URL, concept}   SAM 3.1 camera check-in (objects only, nothing stored)
  POST /tasks/{id}/retry            run a failed task's goal again (an interrupted one is resumed)
  POST /tasks/resume                resume every task cut short when Lantern stopped (M20)
  GET  /reminders, POST /reminders/{id}/cancel   upcoming reminders (M18)
  GET  /doctor                      setup checklist with fixes (M21)
  GET  /settings, POST /settings {persona}   preferences from the desktop (M27; saved next to memory.db)
  GET  /channel/messages            what the simulated phone received (fake channel)
  POST /channel/fake {text?, button?}   act as the owner on the simulated phone
  WS   /events?token=...            live events; every step carries a model badge
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from . import __version__, models
from .approvals import ApprovalError
from .channels import ChannelHub, FakeChannel, WhatsAppChannel, verify_handshake, verify_signature
from .core import Core
from .forms import Vault, VaultError
from .policy import ZONES
from .sam import IDEAS, CheckInError
from .widgets import WidgetError

LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


def load_token(home: Path) -> str:
    """Read or create the local API token (file mode 600)."""
    path = home / "token"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    home.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    try:   # created owner-only in one step: never readable by others, not even briefly
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:   # another Lantern process made it a moment ago
        return path.read_text(encoding="utf-8").strip()
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token)
    return token


class TaskIn(BaseModel):
    goal: str
    source: str = ""   # "mcp" when another program submits it (lantern --mcp)


class DecisionIn(BaseModel):
    nonce: str
    approve: bool
    expected_hash: str | None = None


class ZoneIn(BaseModel):
    path: str
    zone: str


class SettingsIn(BaseModel):
    persona: str


class CheckInIn(BaseModel):
    image: str
    concept: str


class FactIn(BaseModel):
    key: str


class ConfirmIn(BaseModel):
    confirm: str = ""


class PhoneIn(BaseModel):
    text: str = ""
    button: str = ""


class ProfileIn(BaseModel):
    key: str
    value: str
    section: str = "profile"


def create_app(core: Core, token: str, hub: ChannelHub | None = None) -> FastAPI:
    app = FastAPI(title="Lantern", version=__version__, docs_url=None, redoc_url=None)
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^(null|file://.*|app://.*|http://(127\.0\.0\.1|localhost)(:\d+)?)$",
        allow_methods=["GET", "POST"], allow_headers=["X-Lantern-Token", "Content-Type"],
    )

    def auth(x_lantern_token: str = Header(default="")) -> None:
        if not secrets.compare_digest(x_lantern_token, token):
            raise HTTPException(401, "missing or wrong X-Lantern-Token")

    @app.get("/health")
    def health():
        return {"ok": True, "version": __version__, "model": core.cfg.model,
                "badge": models.badge(core.cfg.model), "has_key": bool(os.environ.get(models.API_KEY_ENV)),
                "private_lane": core.router.backend.name, "busy": core.current_task is not None,
                "channels": sorted(hub.channels) if hub else [], "demo": bool(getattr(core, "demo", False))}

    @app.post("/task", dependencies=[Depends(auth)])
    def post_task(body: TaskIn):
        from .tools import EXTERNAL_PREFIX
        from .undo import WORDS as undo_words
        goal = body.goal.strip()
        if body.source == "mcp" and goal:
            goal = EXTERNAL_PREFIX + goal   # tainted from the start; its addresses aren't trusted
        elif undo_words.match(goal):        # "undo" typed in the panel or Quick Ask: like UNDO on the phone
            try:
                return {"task_id": core.undo_last()}
            except ValueError as e:
                raise HTTPException(409, f"Nothing to undo: {e}")
        try:
            return {"task_id": core.submit(goal)}
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/tasks", dependencies=[Depends(auth)])
    def list_tasks(limit: int = 20):
        return [core.task_view(t.id) for t in core.memory.recent_tasks(min(limit, 100))]

    @app.get("/tasks/{task_id}", dependencies=[Depends(auth)])
    def get_task(task_id: int):
        try:
            view = core.task_view(task_id)
        except KeyError:
            raise HTTPException(404, "no such task")
        view["steps"] = core.memory.steps(task_id)
        return view

    @app.get("/tasks/{task_id}/receipt", dependencies=[Depends(auth)])
    def task_receipt(task_id: int):
        from .receipt import as_text, build
        try:
            r = build(core.memory, core.audit, task_id)
        except KeyError:
            raise HTTPException(404, "no such task")
        return {**r, "text": as_text(r)}

    @app.post("/tasks/resume", dependencies=[Depends(auth)])
    def resume_tasks():
        return {"task_ids": core.resume()}

    @app.post("/tasks/{task_id}/retry", dependencies=[Depends(auth)])
    def retry_task(task_id: int):
        try:
            return {"task_id": core.retry_task(task_id)}
        except KeyError:
            raise HTTPException(404, "no such task")
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.post("/tasks/{task_id}/undo", dependencies=[Depends(auth)])
    def undo_task(task_id: int):
        try:
            return {"task_id": core.undo(task_id)}
        except KeyError:
            raise HTTPException(404, "no such task")
        except ValueError as e:   # UndoError: nothing to put back, already undone, still running
            raise HTTPException(409, str(e))

    def settings_view() -> dict:
        from .agent import PERSONAS
        return {"persona": core.agent.persona, "personas": sorted(PERSONAS)}

    @app.get("/settings", dependencies=[Depends(auth)])
    def get_settings():
        return settings_view()

    @app.post("/settings", dependencies=[Depends(auth)])
    def set_settings(body: SettingsIn):
        from . import config as config_mod
        from .agent import PERSONAS
        if body.persona not in PERSONAS:
            raise HTTPException(400, f"persona must be one of {', '.join(sorted(PERSONAS))}")
        core.agent.persona = core.cfg.persona = body.persona   # the next task speaks this way
        config_mod.save_setting("persona", body.persona, Path(core.cfg.db_path).parent)
        return settings_view()

    @app.get("/doctor", dependencies=[Depends(auth)])
    def doctor():
        from .doctor import as_json, run_checks
        return as_json(run_checks(core.cfg))

    @app.get("/reminders", dependencies=[Depends(auth)])
    def reminders():
        return [r.public() for r in core.memory.reminders()]

    @app.post("/reminders/{reminder_id}/cancel", dependencies=[Depends(auth)])
    def cancel_reminder(reminder_id: int):
        try:
            core.memory.get_reminder(reminder_id)
        except KeyError:
            raise HTTPException(404, "no such reminder")
        if not core.cancel_reminder(reminder_id):
            raise HTTPException(409, "that reminder isn't pending")
        return {"cancelled": reminder_id}

    @app.get("/approvals", dependencies=[Depends(auth)])
    def approvals():
        return [a.public(include_nonce=True) for a in core.bus.pending()]

    @app.post("/approvals/{approval_id}", dependencies=[Depends(auth)])
    def decide(approval_id: str, body: DecisionIn):
        try:
            a = core.bus.decide(approval_id, body.nonce, body.approve, by="desktop",
                                expected_hash=body.expected_hash)
        except ApprovalError as e:
            raise HTTPException(409, str(e))
        # The decision itself: by the time this returns, the task may already have used the approval.
        return {**a.public(), "status": "approved" if body.approve else "denied"}

    @app.post("/stop", dependencies=[Depends(auth)])
    def stop():
        return core.stop()

    @app.get("/audit", dependencies=[Depends(auth)])
    def audit(limit: int = 50):
        return {"summary": core.audit.summary(), "entries": core.audit.entries(limit=min(limit, 500))}

    @app.get("/zones", dependencies=[Depends(auth)])
    def zones():
        return {"zones": [{**z, "exists": Path(z["path"]).exists()} for z in core.policy.describe()],
                "choices": list(ZONES)}

    @app.post("/zones", dependencies=[Depends(auth)])
    def set_zone(body: ZoneIn):
        try:
            core.policy.set_zone(body.path, body.zone)
        except ValueError as e:
            raise HTTPException(400, str(e))
        core.policy.save(core.cfg.zones_file)
        return {"zones": core.policy.describe()}

    @app.get("/memory", dependencies=[Depends(auth)])
    def memory():
        return {"facts": core.memory.facts()}

    @app.get("/usage", dependencies=[Depends(auth)])
    def usage():
        return {"by_model": core.memory.usage_by_model(), "total_usd": core.memory.total_cost(),
                "budget": core.budget.status()}

    @app.get("/profile", dependencies=[Depends(auth)])
    def profile():
        return Vault(core.cfg.profile_path).load()

    @app.post("/profile", dependencies=[Depends(auth)])
    def set_profile(body: ProfileIn):
        vault = Vault(core.cfg.profile_path)
        try:
            vault.set(body.key, body.value, body.section)
        except VaultError as e:
            raise HTTPException(400, str(e))
        return vault.load()

    @app.get("/checkin/ideas", dependencies=[Depends(auth)])
    def checkin_ideas():
        return {"ideas": IDEAS}

    @app.post("/checkin", dependencies=[Depends(auth)])
    def checkin(body: CheckInIn):
        try:
            return core.checkin.run(body.image, body.concept)
        except CheckInError as e:
            raise HTTPException(400, str(e))
        except Exception as e:  # no key, network: say so plainly
            raise HTTPException(502, f"check-in failed: {type(e).__name__}: {str(e)[:120]}")

    @app.post("/forget/task/{task_id}", dependencies=[Depends(auth)])
    def forget_task(task_id: int):
        try:
            return core.forget_task(task_id)
        except KeyError:
            raise HTTPException(404, "no such task")
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.post("/forget/fact", dependencies=[Depends(auth)])
    def forget_fact(body: FactIn):
        return {"forgot": body.key, "existed": core.memory.forget(body.key)}

    @app.post("/forget/everything", dependencies=[Depends(auth)])
    def forget_everything(body: ConfirmIn):
        if body.confirm.strip().lower() != "forget everything":
            raise HTTPException(400, 'send {"confirm": "forget everything"}')
        return core.forget_everything()

    @app.get("/widgets", dependencies=[Depends(auth)])
    def widgets():
        return {"widgets": core.widgets.list()}

    @app.get("/widgets/{name}", dependencies=[Depends(auth)])
    def widget(name: str):
        try:
            return {"name": name, "html": core.widgets.get(name)}
        except WidgetError as e:
            raise HTTPException(404 if "no widget" in str(e) else 422, str(e))

    whatsapp = hub.channels.get("whatsapp") if hub else None
    if isinstance(whatsapp, WhatsAppChannel):
        @app.get("/webhook/whatsapp")
        def whatsapp_handshake(request: Request):
            challenge = verify_handshake(dict(request.query_params), whatsapp.verify_token)
            if challenge is None:
                raise HTTPException(403, "verification failed")
            return PlainTextResponse(challenge)

        @app.post("/webhook/whatsapp")
        async def whatsapp_webhook(request: Request):
            raw = await request.body()
            if not verify_signature(raw, request.headers.get("X-Hub-Signature-256"), whatsapp.app_secret):
                raise HTTPException(401, "bad signature")
            try:
                payload = json.loads(raw)
            except ValueError:
                raise HTTPException(400, "not JSON")
            for item in whatsapp.parse(payload):
                hub.receive(item)  # processed on the hub's thread; answer Meta right away
            return {"ok": True}

    phone = hub.channels.get("fake") if hub else None
    if isinstance(phone, FakeChannel):
        @app.get("/channel/messages", dependencies=[Depends(auth)])
        def phone_messages():
            return {"owner": phone.owner, "messages": phone.messages()}

        @app.post("/channel/fake", dependencies=[Depends(auth)])
        def phone_send(body: PhoneIn):
            if not (body.text.strip() or body.button):
                raise HTTPException(400, "send text or a button id")
            phone.note_owner(body.text.strip(), body.button)
            hub.receive(FakeChannel.incoming(text=body.text, button=body.button, sender=phone.owner))
            return {"ok": True}

    @app.websocket("/events")
    async def events(ws: WebSocket, token_q: str = Query(default="", alias="token")):
        if not secrets.compare_digest(token_q, token):
            await ws.close(code=4401)
            return
        await ws.accept()
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)

        def sink(event: dict) -> None:
            loop.call_soon_threadsafe(_put, q, event)

        async def pump() -> None:
            await ws.send_json({"type": "hello", "version": __version__,
                                "pending": [a.public(include_nonce=True) for a in core.bus.pending()],
                                "interrupted": [{"id": t.id, "goal": t.goal.split("\n")[0]}
                                                for t in core.memory.tasks_with_status("interrupted")]})
            while True:
                event = await q.get()
                await ws.send_json(_jsonable(event))

        async def until_closed() -> None:
            # Notice a closed window right away, not at the next event, so no handler (or sink) leaks.
            while (await ws.receive())["type"] != "websocket.disconnect":
                pass

        unsubscribe = core.subscribe(sink)
        tasks = [asyncio.create_task(pump()), asyncio.create_task(until_closed())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            unsubscribe()

    return app


def _put(q: asyncio.Queue, event: dict) -> None:
    if not q.full():
        q.put_nowait(event)


def _jsonable(obj):
    import json
    return json.loads(json.dumps(obj, default=str))


def serve(core: Core, home: Path, host: str = "127.0.0.1", port: int = 8765, hub: ChannelHub | None = None) -> None:
    if host not in LOCAL_HOSTS:
        raise SystemExit("Lantern's server only listens on 127.0.0.1")
    import uvicorn
    token = load_token(home)
    print(f"🏮 Lantern server on http://{host}:{port}  (token in {home / 'token'})")
    if hub:
        print(f"   channels: {', '.join(sorted(hub.channels)) or 'none'}"
              + ("  ·  WhatsApp webhook: https://<your tunnel>/webhook/whatsapp" if "whatsapp" in hub.channels else ""))
    uvicorn.run(create_app(core, token, hub), host=host, port=port, log_level="warning")
