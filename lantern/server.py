"""Lantern's local API: FastAPI on 127.0.0.1 only.

Every route except /health needs the per-install token from ~/.lantern/token
(header `X-Lantern-Token`, or `?token=` for the websocket), so a random web page
in the browser can't drive Lantern through localhost.

  GET  /health                      liveness, model, whether a key is set
  POST /task         {goal}         queue a task -> {task_id}
  GET  /tasks                       recent tasks with status, summary, cost
  GET  /tasks/{id}                  one task with its step timeline
  GET  /approvals                   pending approvals (with nonce, for the approval card)
  POST /approvals/{id} {nonce, approve, expected_hash?}
  POST /stop                        kill switch
  GET  /audit                       what left the laptop today + recent entries
  GET  /zones, POST /zones {path, zone}
  GET  /memory                      remembered facts
  GET  /usage                       tokens and cost per model
  GET  /profile, POST /profile {key, value, section?}   the form profile vault (M7)
  WS   /events?token=...            live events; every step carries a model badge
"""
from __future__ import annotations

import asyncio
import os
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import __version__, models
from .approvals import ApprovalError
from .core import Core
from .forms import Vault, VaultError
from .policy import ZONES

LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


def load_token(home: Path) -> str:
    """Read or create the local API token (file mode 600)."""
    path = home / "token"
    if path.exists():
        return path.read_text().strip()
    home.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    path.write_text(token)
    os.chmod(path, 0o600)
    return token


class TaskIn(BaseModel):
    goal: str


class DecisionIn(BaseModel):
    nonce: str
    approve: bool
    expected_hash: str | None = None


class ZoneIn(BaseModel):
    path: str
    zone: str


class ProfileIn(BaseModel):
    key: str
    value: str
    section: str = "profile"


def create_app(core: Core, token: str) -> FastAPI:
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
                "private_lane": core.router.backend.name, "busy": core.current_task is not None}

    @app.post("/task", dependencies=[Depends(auth)])
    def post_task(body: TaskIn):
        try:
            return {"task_id": core.submit(body.goal)}
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
        return a.public()

    @app.post("/stop", dependencies=[Depends(auth)])
    def stop():
        return core.stop()

    @app.get("/audit", dependencies=[Depends(auth)])
    def audit(limit: int = 50):
        return {"summary": core.audit.summary(), "entries": core.audit.entries(limit=min(limit, 500))}

    @app.get("/zones", dependencies=[Depends(auth)])
    def zones():
        return {"zones": core.policy.describe(), "choices": list(ZONES)}

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
        return {"by_model": core.memory.usage_by_model(), "total_usd": core.memory.total_cost()}

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

        unsubscribe = core.subscribe(sink)
        try:
            await ws.send_json({"type": "hello", "version": __version__,
                                "pending": [a.public(include_nonce=True) for a in core.bus.pending()]})
            while True:
                event = await q.get()
                await ws.send_json(_jsonable(event))
        except WebSocketDisconnect:
            pass
        finally:
            unsubscribe()

    return app


def _put(q: asyncio.Queue, event: dict) -> None:
    if not q.full():
        q.put_nowait(event)


def _jsonable(obj):
    import json
    return json.loads(json.dumps(obj, default=str))


def serve(core: Core, home: Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    if host not in LOCAL_HOSTS:
        raise SystemExit("Lantern's server only listens on 127.0.0.1")
    import uvicorn
    token = load_token(home)
    print(f"🏮 Lantern server on http://{host}:{port}  (token in {home / 'token'})")
    uvicorn.run(create_app(core, token), host=host, port=port, log_level="warning")
