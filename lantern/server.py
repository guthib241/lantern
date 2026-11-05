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
  GET  /stats/time-saved?days=7      honest week total of estimated minutes saved (timesaved.py)
  GET  /profile, POST /profile {key, value, section?}   the form profile vault (M7)
  POST /profile/resume {path}       S15: values for the profile from your own resume (a proposal; nothing saved)
  GET  /webhook/whatsapp            WhatsApp handshake (no token: hub.verify_token instead)
  POST /webhook/whatsapp            WhatsApp messages (no token: X-Hub-Signature-256 instead)
  GET  /widgets, GET /widgets/{name}   sandboxed widgets for the desktop (re-checked on every load)
  GET  /agents, GET /agents/{name}, POST /agents/install, POST /agents/{name}/sign,
  POST /agents/{name}/run, POST /agents/{name}/disable|enable, GET /agents/{name}/export, POST /agents/draft,
  POST /agents/import-skill, GET /agents/{name}/a2a-card,
  POST /agents/{name}/face {emoji}|{generate}, GET /agents/{name}/face   the icon file (T4c)
  GET  /loops, POST /agents/{name}/loop/resume|pause|run-now   loop agents: packs on a schedule (T3c)
  POST /avatar/design {brief, style}   one preview image; POST /avatar/design/{id}/accept
  GET  /avatar/manifest, GET /avatar/file/{id}/{expression}   the active avatar (T4a)
  POST /forget/task/{id}, POST /forget/fact {key}, POST /forget/everything {confirm: "forget everything"}
  POST /checkin {image: data URL, concept}   SAM 3.1 camera check-in (objects only, nothing stored)
  POST /tasks/{id}/retry            run a failed task's goal again (an interrupted one is resumed)
  POST /tasks/resume                resume every task cut short when Lantern stopped (M20)
  GET  /reminders, POST /reminders/{id}/cancel   upcoming reminders (M18)
  GET  /doctor                      setup checklist with fixes (M21)
  GET  /settings, POST /settings {persona}   preferences from the desktop (M27; saved next to memory.db)
  GET  /channel/messages            what the simulated phone received (fake channel)
  POST /channel/fake {text?, button?}   act as the owner on the simulated phone
  POST /lesson {request, notes?}    S16: plan a board lesson (SymPy checks every step) -> {task_id}
  POST /lesson/ask {name, step, question}   S16: answer a question from the board, mid-lesson
  GET  /lessons, GET /lessons/{name}   saved lessons; one lesson as the board plays it
  POST /checkwork {image: data URL} S16: find the first wrong line in handwritten working -> {task_id}
  GET  /checkwork/image/{name}      the photo with that line circled
  GET  /decoy/image/{name}          S7: what you saw / what the cloud model was sent (stand-in values), side by side
  GET  /lend, POST /lend {name, channel, address, text, cedar?, hours?}   S13: loans (preview, then save)
  POST /lend/{id}/revoke            S13: end a loan now
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
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.requests import HTTPConnection
from pydantic import BaseModel, Field

from . import __version__, models, packsign
from .agentpacks import PackError
from .approvals import ApprovalError
from .channels import ChannelHub, FakeChannel, WhatsAppChannel, verify_handshake, verify_signature
from .channels.hub import CHECK_WORK
from .core import Core
from .forms import Vault, VaultError
from .pairing import PairingError, PairingStore
from . import phone as phone_view
from .policy import ZONES, PolicyError
from .sam import IDEAS, CheckInError
from .voice import MAX_BYTES as VOICE_MAX_BYTES, to_wav16k, transcribe
from .widgets import WidgetError

LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")

# blob: only so the phone can show a circled image it fetched with its device token (an <img>
# cannot send that header) and play back a voice note it just recorded; nothing loads from
# another origin, and no script is ever inline or blob-loaded. See docs/DECISIONS.md, Nov 1.
PHONE_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
             "media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; "
             "form-action 'self'")

PHONE_PHOTO_MAX_BYTES = 20_000_000   # like POST /checkwork's data-URL limit
PHONE_VOICE_MAX_BYTES = VOICE_MAX_BYTES
AUDIO_TYPES = {"audio/webm": ".webm", "audio/ogg": ".ogg", "audio/wav": ".wav", "audio/x-wav": ".wav",
               "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/aac": ".aac", "audio/opus": ".opus"}
AUDIO_EXTS = {".webm", ".ogg", ".opus", ".wav", ".mp3", ".m4a", ".mp4", ".aac"}

REPO_ROOT = Path(__file__).resolve().parent.parent
PHONE_DIR = REPO_ROOT / "phone"


FORWARDED = ("forwarded", "x-forwarded-for", "x-real-ip", "cf-connecting-ip", "true-client-ip")


def client_is_local(conn: HTTPConnection) -> bool:
    """Is this request (or websocket) from this computer? Test clients count as local. A request that came through
    a proxy or tunnel (cloudflared, Tailscale Funnel, ngrok) arrives from 127.0.0.1 but carries forwarding headers:
    it is from somewhere else, so it is not local."""
    if any(h in conn.headers for h in FORWARDED):
        return False
    try:
        host = (conn.client.host if conn.client else "") or ""
    except Exception:
        host = ""
    return host in LOCAL_HOSTS or host in ("testclient", "test-client", "")


def lan_ip() -> str:
    """This computer's LAN address for the startup line (best effort)."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def _audio_ext(content_type: str, data: bytes, filename: str = "") -> str | None:
    """The voice note's extension from its file name or type, checked against the bytes when
    they carry a known container signature. None means: not an audio type Lantern accepts."""
    ext = Path(filename or "").suffix.lower()
    if ext not in AUDIO_EXTS:
        ext = AUDIO_TYPES.get((content_type or "").split(";")[0].strip().lower(), "")
    if not ext:
        if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
            ext = ".wav"
        elif data[:4] == b"OggS":
            ext = ".ogg"
        elif data[:4] == b"\x1aE\xdf\xa3":
            ext = ".webm"
        else:
            return None
    if ext == ".wav" and not (data[:4] == b"RIFF" and data[8:12] == b"WAVE"):
        return None   # a .wav name on non-WAV bytes: ffmpeg would only fail later and less plainly
    return ext


def _voice_error(e: Exception) -> str:
    """A plain sentence for a failed voice note, naming the missing piece when there is one."""
    msg = str(e)
    if "ffmpeg is not installed" in msg:
        return "I couldn't hear that voice note: ffmpeg is not installed on this computer, so I can't convert it."
    if models.API_KEY_ENV in msg:
        return (f"I couldn't hear that voice note: no model key is set ({models.API_KEY_ENV}), "
                "so it can't be transcribed yet.")
    return f"I couldn't hear that voice note ({type(e).__name__}). Try again or type it."


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


SKINS = ("muse", "orb", "glow", "clock")   # Lantern's looks (T4b): its Muse face, the orb, the glow, a live clock


class SettingsIn(BaseModel):
    persona: str | None = None
    skin: str | None = None
    localsend: bool | None = None


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


class AnswerIn(BaseModel):
    answer: str


class LearnIn(BaseModel):
    task_id: int


class NetContactIn(BaseModel):
    card: dict
    channel: str
    address: str


class NetRulesIn(BaseModel):
    contact: str
    text: str
    cedar: str = ""


class NetAskIn(BaseModel):
    contact: str
    query: str = ""
    routine: str = ""


class ReelIn(BaseModel):
    path: str
    words: str = ""


class LendIn(BaseModel):
    name: str
    channel: str
    address: str
    text: str
    cedar: str = ""      # set: save this previewed policy
    hours: float = 0


class LessonIn(BaseModel):
    request: str
    notes: str = ""


class LessonAskIn(BaseModel):
    name: str
    step: int
    question: str


class CheckWorkIn(BaseModel):
    image: str


class TeachIn(BaseModel):
    url: str
    goal: str


class TeachNoteIn(BaseModel):
    task_id: int | None = None
    text: str = ""


class ProfileIn(BaseModel):
    key: str
    value: str
    section: str = "profile"


class ResumeIn(BaseModel):
    path: str


class MeetingIn(BaseModel):
    path: str


class MeetingConfirmIn(BaseModel):
    id: str
    picks: list[int] = Field(default_factory=list)


class PairRedeemIn(BaseModel):
    code: str = ""
    name: str = "phone"


class PhoneTaskIn(BaseModel):
    goal: str


def _submit_goal(core: Core, goal: str) -> int:
    """Queue a goal exactly like POST /task (undo words become an undo task)."""
    from .tools import EXTERNAL_PREFIX
    from .undo import WORDS as undo_words
    goal = (goal or "").strip()
    if undo_words.match(goal):
        try:
            return core.undo_last()
        except ValueError as e:
            raise HTTPException(409, f"Nothing to undo: {e}")
    if goal.startswith(EXTERNAL_PREFIX):
        pass
    try:
        return core.submit(goal)
    except ValueError as e:
        raise HTTPException(400, str(e))


class PackRunIn(BaseModel):
    inputs: dict = {}


class SurfaceIn(BaseModel):
    surface: str = ""


class PackDraftIn(BaseModel):
    task_id: int


class SkillImportIn(BaseModel):
    path: str | None = None
    text: str | None = None
    filename: str = "SKILL.md"


class AvatarDesignIn(BaseModel):
    brief: str = ""
    style: str = ""


class AgentFaceIn(BaseModel):
    emoji: str | None = None
    generate: bool = False


def create_app(core: Core, token: str, hub: ChannelHub | None = None, *,
               pairing: PairingStore | None = None, phone_mode: bool = False,
               phone_dir: Path | str | None = None) -> FastAPI:
    app = FastAPI(title="Lantern", version=__version__, docs_url=None, redoc_url=None)
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^(null|file://.*|app://.*|http://(127\.0\.0\.1|localhost)(:\d+)?)$",
        allow_methods=["GET", "POST"], allow_headers=["X-Lantern-Token", "Content-Type"],
    )

    def auth(x_lantern_token: str = Header(default="")) -> None:
        if not secrets.compare_digest(x_lantern_token, token):
            raise HTTPException(401, "missing or wrong X-Lantern-Token")

    store = pairing
    if store is None:
        try:
            home = Path(core.cfg.db_path).expanduser().parent
        except Exception:
            from . import config as config_mod
            home = config_mod.HOME
        store = PairingStore(home / "devices.json")
    app.state.pairing = store
    app.state.phone_mode = bool(phone_mode)

    def device_auth(x_lantern_device: str = Header(default="")) -> dict:
        found = app.state.pairing.verify(x_lantern_device or "")
        if found is None:
            raise HTTPException(401, "unknown or revoked device; pair again from the desktop")
        return found

    LAN_OPEN = ("/health", "/pair", "/m", "/m/")
    LAN_PREFIXES = ("/m/", "/webhook/")

    @app.middleware("http")
    async def lan_guard(request: Request, call_next):
        if app.state.phone_mode and not client_is_local(request):
            path = request.url.path
            open_path = (path in LAN_OPEN or path == "/pair"
                         or any(path.startswith(p) for p in LAN_PREFIXES)
                         or path.startswith("/m?") or path == "/m/events")
            if not open_path:
                return PlainTextResponse("localhost only", status_code=403)
            if path == "/pair/start" or path.startswith("/devices"):
                return PlainTextResponse("localhost only", status_code=403)
        resp = await call_next(request)
        if request.url.path == "/m" or request.url.path.startswith("/m/"):
            resp.headers["Content-Security-Policy"] = PHONE_CSP
        return resp

    @app.post("/pair/start", dependencies=[Depends(auth)])
    def pair_start(request: Request):
        code, expires_in = app.state.pairing.start()
        return {"code": code, "expires_in": expires_in, "url": "/m"}

    @app.post("/pair")
    def pair_redeem(body: PairRedeemIn):
        try:
            device_token, device_id = app.state.pairing.redeem(body.code, body.name)
        except PairingError as e:
            raise HTTPException(409 if "wrong" in str(e).lower() else 400, str(e))
        return {"device_token": device_token, "device_id": device_id}

    @app.get("/devices", dependencies=[Depends(auth)])
    def devices():
        return {"devices": app.state.pairing.list()}

    @app.post("/devices/{device_id}/revoke", dependencies=[Depends(auth)])
    def device_revoke(device_id: str):
        try:
            return app.state.pairing.revoke(device_id)
        except KeyError:
            raise HTTPException(404, "no such device")
        except ValueError as e:
            raise HTTPException(409, str(e))

    # ---- phone API (paired device token; WhatsApp-level privacy) ---------------------------
    @app.post("/m/api/task")
    def phone_task(body: PhoneTaskIn, _dev: dict = Depends(device_auth)):
        return {"task_id": _submit_goal(core, body.goal)}

    async def _upload(request: Request, file_fields: tuple[str, ...], max_bytes: int,
                      too_large: str) -> tuple[bytes, dict, str, str]:
        """One uploaded file, as (bytes, text fields, filename, content type). The phone app sends
        the raw file as the body; a multipart form with a file part is accepted too, so the route
        can be driven by any HTTP client. Nothing about the file is trusted from either shape.

        Size is refused early, before the body is held whole in memory: a declared
        Content-Length over the limit is rejected without reading at all (the only check a
        multipart body gets, before it is parsed), and a raw body is streamed and rejected
        the moment the bytes read pass the limit, whatever its headers claimed."""
        declared = request.headers.get("content-length")
        if declared:
            try:
                declared_n = int(declared)
            except ValueError:
                declared_n = None   # a nonsense header proves nothing; the stream check still applies
            if declared_n is not None and declared_n > max_bytes:
                raise HTTPException(413, too_large)
        ctype = request.headers.get("content-type", "")
        if ctype.startswith("multipart/form-data"):
            from starlette.datastructures import UploadFile
            try:
                form = await request.form()
            except Exception:
                raise HTTPException(400, "send the file as multipart form data or as the request body")
            data, filename, fields = None, "", {}
            for key, value in form.multi_items():
                if isinstance(value, UploadFile):
                    if data is None and (key in file_fields or not file_fields):
                        data, filename = await value.read(), value.filename or ""
                elif key not in fields:
                    fields[key] = str(value)
            if data is None:
                raise HTTPException(400, "send the file itself")
            if len(data) > max_bytes:   # a chunked multipart body had no length to check up front
                raise HTTPException(413, too_large)
            return data, fields, filename, ctype
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > max_bytes:
                raise HTTPException(413, too_large)   # stop reading; the rest is never buffered
            body += chunk
        return bytes(body), {}, "", ctype

    def _transcribe_phone_voice(data: bytes, ext: str) -> dict:
        """A phone voice note through exactly the WhatsApp pipeline (hub.py `_voice`): kept in
        Lantern's media folder, ffmpeg to 16 kHz WAV, then Muse Voice Transcribe's file endpoint
        (voice.transcribe), biased toward the names Lantern knows, audited and costed the same way.
        In rehearsal the hub already carries the scripted converter/transcriber (demo.build); a
        demo core with no hub gets the scripted transcript directly, so no key or ffmpeg is needed."""
        import datetime as _dt
        if hub is None and getattr(core, "demo", False):
            from .demo import STORY
            return {"transcript": STORY[0].goal, "audioDurationMs": 0}
        media = Path(core.cfg.media_dir).expanduser()
        media.mkdir(parents=True, exist_ok=True)
        stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")   # short digit runs: never card-shaped
        src = media / f"phone-voice-{stamp}{ext}"
        src.write_bytes(data)
        converter = getattr(hub, "converter", None) or to_wav16k
        transcriber = getattr(hub, "transcriber", None) or transcribe
        keywords = hub.keywords() if hub is not None and hasattr(hub, "keywords") else []
        wav = converter(src, src.with_name(src.stem + ".16k.wav"))   # never onto itself (.wav in)
        core.audit.record(lane="cloud", destination=getattr(core.cfg, "base_url", models.BASE_URL),
                          endpoint="asr.transcribe",
                          model=models.VOICE, payload=Path(wav).read_bytes(), preview="[voice note]")
        result = transcriber(wav, keywords=keywords)
        ms = int(result.get("audioDurationMs") or 0)
        cost = models.audio_cost(ms)
        core.memory.log_usage(None, models.VOICE, cost_usd=cost)
        core.emit({"type": "usage", "model": models.VOICE, "badge": models.badge(models.VOICE),
                   "audio_ms": ms, "cost_usd": cost})
        return result

    @app.post("/m/api/photo")
    async def phone_photo(request: Request, text: str = "", _dev: dict = Depends(device_auth)):
        """A photo from the phone, with the words in the message box as its caption: "check my
        work" goes to check-work, anything else to the scan pipeline (hub.py `_start_photo`,
        core.photo / core.check_work, unchanged). The type is sniffed from the bytes."""
        data, fields, _name, _ctype = await _upload(
            request, ("image", "file", "photo"), PHONE_PHOTO_MAX_BYTES,
            f"that photo is over {PHONE_PHOTO_MAX_BYTES // 1_000_000} MB")
        if not data:
            raise HTTPException(400, "send the photo itself")
        if len(data) > PHONE_PHOTO_MAX_BYTES:
            raise HTTPException(413, f"that photo is over {PHONE_PHOTO_MAX_BYTES // 1_000_000} MB")
        sniffed = phone_view.sniff_image(data)
        if sniffed is None:
            raise HTTPException(415, "send a JPEG, PNG or WebP photo (checked from the file itself, "
                                     "not from its name)")
        words = " ".join(str(text or fields.get("text") or fields.get("caption") or "").split())[:2000]
        try:
            # Off the event loop: scanning / checking a photo takes seconds of model work to
            # queue up, and every other request and websocket must stay responsive meanwhile.
            if CHECK_WORK.search(words):
                task_id = await asyncio.to_thread(core.check_work, data)
            else:
                task_id = await asyncio.to_thread(core.photo, data, words)
        except ValueError as e:
            raise HTTPException(409, str(e))
        return {"task_id": task_id, "image_type": sniffed[1]}

    @app.post("/m/api/voice")
    async def phone_voice(request: Request, _dev: dict = Depends(device_auth)):
        """A voice note from the phone: transcribed exactly like a WhatsApp voice note, shown back
        as the user's message ("🎙 …"), then handled like typed text (the same _submit_goal)."""
        data, fields, filename, ctype = await _upload(
            request, ("audio", "file", "voice"), PHONE_VOICE_MAX_BYTES,
            "that voice note is too long (Muse Voice Transcribe takes at most 10 minutes or 32 MB)")
        if not data:
            raise HTTPException(400, "send the voice note itself")
        if len(data) > PHONE_VOICE_MAX_BYTES:
            raise HTTPException(413, "that voice note is too long (Muse Voice Transcribe takes "
                                     "at most 10 minutes or 32 MB)")
        ext = _audio_ext(fields.get("content_type", "") or ctype, data, filename)
        if ext is None:
            raise HTTPException(415, "send a voice note in a format Lantern can convert "
                                     "(WebM, OGG, WAV, MP3 or M4A from the recorder)")
        try:
            # Off the event loop: ffmpeg and the transcription call take seconds, during which
            # every other request and websocket (the desktop's /events included) must stay live.
            result = await asyncio.to_thread(_transcribe_phone_voice, data, ext)
        except HTTPException:
            raise
        except Exception as e:   # download, ffmpeg, transcription: all end the same plain way
            raise HTTPException(503, _voice_error(e))
        text = str(result.get("transcript", "")).strip()
        if not text:
            raise HTTPException(422, "I got the voice note but couldn't make out any words.")
        task_id = _submit_goal(core, text)
        return {"task_id": task_id, "transcript": text, "message": f"🎙 {text}",
                "audio_ms": int(result.get("audioDurationMs") or 0)}

    @app.get("/m/api/tasks/{task_id}/image")
    def phone_task_image(task_id: int, _dev: dict = Depends(device_auth)):
        """This task's circled check-work image, and nothing else (phone.task_image_path holds the
        media-folder / own-task / private checks)."""
        try:
            path = phone_view.task_image_path(core, task_id)
        except KeyError:
            raise HTTPException(404, "no such task")
        except PermissionError:
            raise HTTPException(403, "that task is private; its image stays on this computer")
        if path is None:
            raise HTTPException(404, "that task made no circled image")
        media = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        return FileResponse(path, media_type=media)

    @app.get("/m/api/tasks")
    def phone_tasks(limit: int = 20, _dev: dict = Depends(device_auth)):
        return phone_view.tasks_view(core, limit)

    @app.get("/m/api/tasks/{task_id}")
    def phone_task_view(task_id: int, _dev: dict = Depends(device_auth)):
        try:
            return phone_view.task_view(core, task_id)
        except KeyError:
            raise HTTPException(404, "no such task")

    @app.get("/m/api/approvals")
    def phone_approvals(_dev: dict = Depends(device_auth)):
        import time as _t
        return [phone_view.approval_view(a, _t.time()) for a in core.bus.pending()]

    @app.post("/m/api/approvals/{approval_id}")
    def phone_decide(approval_id: str, body: DecisionIn, _dev: dict = Depends(device_auth)):
        try:
            a = core.bus.decide(approval_id, body.nonce, body.approve, by="phone",
                                expected_hash=body.expected_hash)
        except ApprovalError as e:
            raise HTTPException(409, str(e))
        return {**phone_view.approval_view(a), "status": "approved" if body.approve else "denied"}

    @app.get("/m/api/reminders")
    def phone_reminders(_dev: dict = Depends(device_auth)):
        return [r.public() for r in core.memory.reminders()]

    @app.post("/m/api/reminders/{reminder_id}/cancel")
    def phone_cancel_reminder(reminder_id: int, _dev: dict = Depends(device_auth)):
        try:
            core.memory.get_reminder(reminder_id)
        except KeyError:
            raise HTTPException(404, "no such reminder")
        if not core.cancel_reminder(reminder_id):
            raise HTTPException(409, "that reminder isn't pending")
        return {"cancelled": reminder_id}

    @app.post("/m/api/stop")
    def phone_stop(_dev: dict = Depends(device_auth)):
        return core.stop()

    @app.websocket("/m/events")
    async def phone_events(ws: WebSocket, device: str = Query(default="")):
        if app.state.pairing.verify(device or "") is None:
            await ws.close(code=4401)
            return
        await ws.accept()
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)

        def sink(event: dict) -> None:
            loop.call_soon_threadsafe(_put, q, event)

        def filtered(event: dict) -> dict:
            kind = event.get("type")
            if kind == "approval" and isinstance(event.get("approval"), dict):
                if event.get("event") == "requested":
                    return {"type": "approval", "event": "requested",
                            "approval": phone_view.approval_view(event["approval"]),
                            "task_id": event.get("task_id")}
                return {"type": "approval", "event": event.get("event"),
                        "approval": {"id": event["approval"].get("id"), "status": event["approval"].get("status")},
                        "task_id": event.get("task_id")}
            if kind == "done":
                view: dict = {"type": "done", "id": event.get("id"), "status": event.get("status"),
                              "task_id": event.get("id")}
                reply = (event.get("reply_public") or "").strip() if "reply_public" in event else None
                if reply is None:
                    try:
                        reply = phone_view.task_view(core, int(event.get("id")))["reply"] if event.get("id") else ""
                    except Exception:
                        reply = str(event.get("summary") or "")
                view["reply"] = reply or ""
                return view
            return _jsonable(event)

        async def pump() -> None:
            import time as _t
            await ws.send_json({"type": "hello", "version": __version__,
                                "pending": [phone_view.approval_view(a, _t.time()) for a in core.bus.pending()]})
            while True:
                event = await q.get()
                await ws.send_json(filtered(event))

        async def until_closed() -> None:
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

    @app.get("/m/face")
    def phone_face():
        candidates = [REPO_ROOT / "desktop" / "assets" / "avatar" / "idle.webp",
                      REPO_ROOT / "desktop" / "assets" / "avatar" / "idle.png"]
        for path in candidates:
            if path.is_file():
                media = "image/webp" if path.suffix == ".webp" else "image/png"
                return FileResponse(path, media_type=media)
        raise HTTPException(404, "no face yet")

    @app.get("/m")
    def phone_index():
        index = (Path(phone_dir) if phone_dir else PHONE_DIR) / "index.html"
        if not index.is_file():
            raise HTTPException(404, "phone app not built")
        return FileResponse(index, media_type="text/html", headers={"Content-Security-Policy": PHONE_CSP})

    @app.get("/health")
    def health():
        return {"ok": True, "version": __version__, "model": core.cfg.model,
                "badge": models.badge(core.cfg.model), "has_key": bool(os.environ.get(models.API_KEY_ENV)),
                "private_lane": core.router.backend.name, "busy": core.current_task is not None or bool(core.scheduler and core.scheduler.busy()),
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

    @app.post("/tasks/{task_id}/answer", dependencies=[Depends(auth)])
    def answer_task(task_id: int, body: AnswerIn):
        try:
            core.answer(task_id, body.answer, by="desktop")
        except ValueError as e:   # not waiting for an answer, or an empty answer
            raise HTTPException(409, str(e))
        return {"task_id": task_id, "answered": True}

    @app.get("/questions", dependencies=[Depends(auth)])
    def questions():
        return core.questions()

    @app.get("/routines", dependencies=[Depends(auth)])
    def routines():
        return core.routines()

    @app.post("/routines/learn", dependencies=[Depends(auth)])
    def learn(body: LearnIn):
        try:
            return {"task_id": core.learn(body.task_id)}
        except KeyError:
            raise HTTPException(404, "no such task")
        except ValueError as e:
            raise HTTPException(409, str(e))

    # ---- Lantern Network (S6) -----------------------------------------------------------------------------
    @app.get("/network", dependencies=[Depends(auth)])
    def network():
        me, contacts, _ = core.net()
        return {"card": me.card(), "contacts": [{k: c[k] for k in ("name", "fp", "channel", "address")}
                                                | {"rules": (c.get("rules") or {}).get("text", "")}
                                                for c in contacts.all().values()]}

    @app.post("/network/contacts", dependencies=[Depends(auth)])
    def network_add(body: NetContactIn):
        from .network import NetError
        try:
            c = core.network_add(body.card, body.channel, body.address)
        except NetError as e:
            raise HTTPException(400, str(e))
        return {k: c[k] for k in ("name", "fp", "channel", "address")}

    @app.post("/network/rules", dependencies=[Depends(auth)])
    def network_rules(body: NetRulesIn):
        """Preview: the policy and what the engine decides for example requests. With `cedar`: save that policy."""
        from .network import NetError
        try:
            return core.network_rules(body.contact, body.text, save=bool(body.cedar), cedar=body.cedar)
        except NetError as e:
            raise HTTPException(409, str(e))

    @app.post("/network/ask", dependencies=[Depends(auth)])
    def network_ask(body: NetAskIn):
        from .network import NetError
        try:
            if body.routine:
                return {"task_id": core.network_share(body.contact, body.routine)}
            return {"task_id": core.network_ask(body.contact, body.query)}
        except KeyError:
            raise HTTPException(404, "no such routine")
        except NetError as e:
            raise HTTPException(409, str(e))

    @app.post("/reel", dependencies=[Depends(auth)])
    def reel(body: ReelIn):
        """S12: a how-to video file on this computer: the steps come back as one card to approve."""
        try:
            return {"task_id": core.reel(path=body.path, words=body.words)}
        except (ValueError, PolicyError) as e:
            raise HTTPException(409, str(e))

    # ---- Lend your Lantern (S13) ------------------------------------------------------------------------------
    @app.get("/lend", dependencies=[Depends(auth)])
    def lend_list():
        from .lend import active
        return [{**{k: g[k] for k in ("id", "name", "channel", "address", "text", "expires_at", "status", "tasks")},
                 "active": active(g)} for g in core.lending().all().values()]

    @app.post("/lend", dependencies=[Depends(auth)])
    def lend(body: LendIn):
        """Preview what the words become (policy, hours, the engine's decisions); with `cedar`, save that grant."""
        from .lend import LendError
        from .network import NetError
        try:
            return core.lend(body.name, body.channel, body.address, body.text, save=bool(body.cedar),
                             cedar=body.cedar, hours=body.hours)
        except (LendError, NetError) as e:
            raise HTTPException(409, str(e))

    @app.post("/lend/{grant_id}/revoke", dependencies=[Depends(auth)])
    def lend_revoke(grant_id: str):
        from .lend import LendError
        try:
            g = core.revoke(grant_id)
        except LendError:
            raise HTTPException(404, "no such loan")
        return {"id": g["id"], "status": g["status"]}

    # ---- Teaching Mode (S16) --------------------------------------------------------------------------------
    @app.post("/lesson", dependencies=[Depends(auth)])
    def lesson(body: LessonIn):
        from .lesson import LessonError
        try:
            return {"task_id": core.lesson(body.request, body.notes)}
        except (LessonError, ValueError, PolicyError) as e:
            raise HTTPException(409, str(e))

    @app.post("/lesson/ask", dependencies=[Depends(auth)])
    def lesson_ask(body: LessonAskIn):
        """S16 voice interruption: a question from the board about the step it is on. The answer is
        spoken there and may add up to 3 SymPy-checked board actions; the lesson then resumes."""
        from .lesson import load
        from .lesson_ask import LessonAskError
        try:
            saved = load(core.lessons_folder(), body.name)
        except KeyError:
            raise HTTPException(404, "no such lesson")
        try:
            return core.lesson_ask(saved["lesson"], body.step, body.question)
        except LessonAskError as e:
            raise HTTPException(409, str(e))

    @app.get("/lessons", dependencies=[Depends(auth)])
    def lessons():
        from .lesson import listing
        return listing(core.lessons_folder())

    @app.get("/lessons/{name}", dependencies=[Depends(auth)])
    def lesson_get(name: str):
        from .lesson import load
        try:
            return load(core.lessons_folder(), name)
        except KeyError:
            raise HTTPException(404, "no such lesson")

    @app.post("/checkwork", dependencies=[Depends(auth)])
    def checkwork(body: CheckWorkIn):
        import base64
        import re as _re
        m = _re.fullmatch(r"data:image/(?:jpeg|png|webp);base64,([A-Za-z0-9+/=]+)", body.image or "")
        if not m or len(body.image) > 20_000_000:
            raise HTTPException(400, "send one JPEG, PNG or WebP photo as a data URL")
        try:
            return {"task_id": core.check_work(base64.b64decode(m.group(1)))}
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.get("/checkwork/image/{name}", dependencies=[Depends(auth)])
    def checkwork_image(name: str):
        import re as _re
        from fastapi.responses import FileResponse
        path = Path(core.cfg.media_dir).expanduser() / name
        if not _re.fullmatch(r"checked-[0-9-]+\.png", name) or not path.is_file():
            raise HTTPException(404, "no such image")
        return FileResponse(path, media_type="image/png")

    @app.get("/decoy/image/{name}", dependencies=[Depends(auth)])
    def decoy_image(name: str):
        """S7: one half of a screenshot pair from decoy mode (local only; the "seen" half never left)."""
        import re as _re
        from fastapi.responses import FileResponse
        path = Path(core.cfg.media_dir).expanduser() / name
        if not _re.fullmatch(r"decoy_\d{8}_\d{6}_[0-9a-f]{6}-(?:seen|sent)\.jpg", name) or not path.is_file():
            raise HTTPException(404, "no such image")
        return FileResponse(path, media_type="image/jpeg")

    @app.post("/sleep", dependencies=[Depends(auth)])
    def sleep_now():
        """S11: check and repair every learned routine now (it also runs each night)."""
        try:
            return {"task_id": core.sleep()}
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.post("/teach", dependencies=[Depends(auth)])
    def teach(body: TeachIn):
        """S8: open Lantern's browser and watch the user do the task once; then learn it."""
        try:
            return {"task_id": core.teach(body.url, body.goal)}
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.post("/teach/stop", dependencies=[Depends(auth)])
    def teach_stop(body: TeachNoteIn):
        return {"stopped": core.teach_stop(body.task_id)}

    @app.post("/teach/note", dependencies=[Depends(auth)])
    def teach_note(body: TeachNoteIn):
        return {"noted": core.teach_note(body.task_id, body.text)}

    @app.post("/routines/{name}/forget", dependencies=[Depends(auth)])
    def forget_routine(name: str):
        try:
            return {"forgot": core.forget_routine(name)}
        except KeyError:
            raise HTTPException(404, "no such routine")

    @app.get("/router", dependencies=[Depends(auth)])
    def router_table():
        from .ladder import RUNGS, Ladder
        return {"rungs": [{"n": r.n, "name": r.name, "badge": r.badge, "cloud": r.cloud} for r in RUNGS],
                "table": Ladder(core.memory).table()}

    @app.get("/scheduler", dependencies=[Depends(auth)])
    def scheduler_view():
        snap = core.scheduler.snapshot() if core.scheduler is not None else {"workers": 0}
        return {**snap, "questions": core.questions()}

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
        return {"persona": core.agent.persona, "personas": sorted(PERSONAS),
                "skin": core.cfg.skin, "skins": list(SKINS),
                "localsend": bool(core.localsend and core.localsend.listening)}

    @app.get("/settings", dependencies=[Depends(auth)])
    def get_settings():
        return settings_view()

    @app.post("/settings", dependencies=[Depends(auth)])
    def set_settings(body: SettingsIn):
        from . import config as config_mod
        from .agent import PERSONAS
        if body.persona is None and body.skin is None and body.localsend is None:
            raise HTTPException(400, "nothing to change: give a persona, a skin and/or localsend")
        if body.persona is not None:
            if body.persona not in PERSONAS:
                raise HTTPException(400, f"persona must be one of {', '.join(sorted(PERSONAS))}")
            core.agent.persona = core.cfg.persona = body.persona   # the next task speaks this way
            config_mod.save_setting("persona", body.persona, Path(core.cfg.db_path).parent)
        if body.skin is not None:
            if body.skin not in SKINS:
                raise HTTPException(400, f"skin must be one of {', '.join(SKINS)}")
            core.cfg.skin = body.skin   # free and instant: no model call
            config_mod.save_setting("skin", body.skin, Path(core.cfg.db_path).parent)
            core.emit({"type": "settings", "skin": body.skin})   # the avatar window changes look at once
        if body.localsend is not None:   # Setup -> "Receive from phones on Wi-Fi"
            core.cfg.localsend = body.localsend
            config_mod.save_setting("localsend", body.localsend, Path(core.cfg.db_path).parent)
            try:
                if body.localsend:
                    core.start_localsend()
                else:
                    core.stop_localsend()
            except RuntimeError as e:   # e.g. the inbox is not in a writable zone
                raise HTTPException(409, str(e))
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

    @app.get("/stats/time-saved", dependencies=[Depends(auth)])
    def time_saved(days: int = 7):
        """Honest week total: stored per-task estimates (verified actions only,
        fixed table) plus verified LocalSend receives, per local day."""
        from . import timesaved
        try:
            return timesaved.summarize(core.memory, core.audit, days=days)
        except ValueError as e:
            raise HTTPException(400, str(e))

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

    @app.post("/profile/resume", dependencies=[Depends(auth)])
    def profile_from_resume(body: ResumeIn):
        """S15: Muse Spark reads your own resume and proposes profile values; you save the ones you choose."""
        from .cv import ResumeError, propose
        try:
            return propose(core.client, core.policy, body.path, Vault(core.cfg.profile_path), memory=core.memory)
        except (ResumeError, PolicyError) as e:
            raise HTTPException(409, str(e))

    @app.post("/meeting", dependencies=[Depends(auth)])
    def meeting(body: MeetingIn):
        """S15: a meeting recording → who said what, and proposed tasks and reminders; nothing is done yet."""
        from .meeting import MeetingError
        try:
            return core.meeting(body.path)
        except (MeetingError, PolicyError) as e:
            raise HTTPException(409, str(e))

    @app.post("/meeting/confirm", dependencies=[Depends(auth)])
    def meeting_confirm(body: MeetingConfirmIn):
        """The items the user ticked: reminders are saved, tasks start as the user's own requests."""
        try:
            return core.confirm_meeting(body.id, body.picks)
        except ValueError as e:
            raise HTTPException(409, str(e))

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

    @app.get("/agents", dependencies=[Depends(auth)])
    def list_agents():
        out = []
        for entry in core.packs.list():
            out.append({**entry, "mine": core.pack_author_is_you(entry.get("author", ""))})
        return {"agents": out}

    @app.get("/agents/{name}", dependencies=[Depends(auth)])
    def get_agent(name: str):
        try:
            pack = core.packs.get(name)   # re-validated: a changed file answers 422, not a stale pack
        except KeyError:
            raise HTTPException(404, f"no agent called {name!r}")
        except PackError as e:
            raise HTTPException(422, str(e))
        info = packsign.signature_info(pack.as_dict())
        return {**pack.as_dict(), "disabled": pack.disabled, "face": core.packs.face(name),
                "loop": core.packs.loop_state(name), "surface": core.pack_surface(name),
                "signature_status": info["status"], "fingerprint": info["fingerprint"],
                "mine": core.pack_author_is_you(pack.author)}

    @app.post("/agents/install", dependencies=[Depends(auth)])
    def install_agent(body: dict, replace: bool = False):
        """Install a pack file. Blocks until the install card is approved or declined."""
        try:
            return core.pack_install(body, replace=replace)
        except PackError as e:
            raise HTTPException(400, str(e))

    @app.post("/agents/draft", dependencies=[Depends(auth)])
    def draft_agent(body: PackDraftIn):
        """Draft a pack from a finished task. The panel lets the user edit it;
        saving goes through install (with approval)."""
        try:
            return core.pack_draft(body.task_id)
        except ValueError as e:
            raise HTTPException(404 if str(e).startswith("no task") else 502, str(e))

    @app.post("/agents/import-skill", dependencies=[Depends(auth)])
    def import_skill(body: SkillImportIn):
        """Preview a SKILL.md import: the validated pack, skipped scripts and
        whether the body was cut. The panel's tool checkboxes land when the
        preview is saved through install (with its approval)."""
        from .skillimport import SkillImportError
        try:
            return core.pack_import_skill({"path": body.path, "text": body.text,
                                           "filename": body.filename})
        except SkillImportError as e:
            raise HTTPException(400, str(e))

    @app.get("/agents/{name}/a2a-card", dependencies=[Depends(auth)])
    def a2a_card(name: str):
        """This installed pack as an A2A Agent Card (`agent-card.json`), so
        other agent tools can discover it. No endpoint URL is published."""
        try:
            return core.pack_a2a_card(name)
        except KeyError:
            raise HTTPException(404, f"no agent called {name!r}")
        except PackError as e:
            raise HTTPException(422, str(e))

    @app.post("/agents/{name}/run", dependencies=[Depends(auth)])
    def run_agent(name: str, body: PackRunIn):
        try:
            return {"task_id": core.submit("", pack=name, inputs=body.inputs)}
        except KeyError:
            raise HTTPException(404, f"no agent called {name!r}")
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/agents/{name}/surface", dependencies=[Depends(auth)])
    def get_surface(name: str):
        from .agentpacks import PackError as _PackError
        try:
            return {"name": name, "surface": core.pack_surface(name)}
        except KeyError:
            raise HTTPException(404, f"no agent called {name!r}")
        except _PackError as e:
            raise HTTPException(422, str(e))

    @app.post("/agents/{name}/surface", dependencies=[Depends(auth)])
    def set_surface(name: str, body: SurfaceIn):
        """Pick where an agent runs (laptop, phone or both). Local-only, like
        enable/disable: it spends nothing, so no approval card."""
        from .agentpacks import PackError as _PackError
        try:
            return core.set_pack_surface(name, body.surface)
        except KeyError:
            raise HTTPException(404, f"no agent called {name!r}")
        except _PackError as e:
            raise HTTPException(422, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/agents/{name}/sign", dependencies=[Depends(auth)])
    def sign_agent(name: str):
        """Sign your own installed pack with this laptop's key. Blocks until approved."""
        try:
            return core.pack_sign(name)
        except KeyError:
            raise HTTPException(404, f"no agent called {name!r}")
        except PackError as e:
            raise HTTPException(422, str(e))

    @app.get("/loops", dependencies=[Depends(auth)])
    def list_loops():
        """Loop status for every installed pack that suggests a schedule."""
        return {"loops": core.loops()}

    @app.post("/agents/{name}/loop/resume", dependencies=[Depends(auth)])
    def resume_loop(name: str):
        """Turn a loop on. Blocks until the enable card (how often, per-run budget) is approved."""
        from .agentpacks import PackError as _PackError
        try:
            return core.loop_resume(name)
        except KeyError:
            raise HTTPException(404, f"no agent called {name!r}")
        except _PackError as e:
            raise HTTPException(422, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/agents/{name}/loop/pause", dependencies=[Depends(auth)])
    def pause_loop(name: str):
        """Turn a loop off. No approval: pausing spends nothing."""
        try:
            return core.loop_pause(name)
        except KeyError:
            raise HTTPException(404, f"no agent called {name!r}")

    @app.post("/agents/{name}/loop/run-now", dependencies=[Depends(auth)])
    def run_loop_now(name: str):
        """Start this loop's scheduled run right away, as an ordinary task."""
        from .agentpacks import PackError as _PackError
        try:
            return {"task_id": core.loop_run_now(name)}
        except KeyError:
            raise HTTPException(404, f"no agent called {name!r}")
        except _PackError as e:
            raise HTTPException(422, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/agents/{name}/disable", dependencies=[Depends(auth)])
    def disable_agent(name: str):
        try:
            pack = core.packs.disable(name)
        except KeyError:
            raise HTTPException(404, f"no agent called {name!r}")
        return {"name": pack.name, "disabled": True}

    @app.post("/agents/{name}/enable", dependencies=[Depends(auth)])
    def enable_agent(name: str):
        try:
            pack = core.packs.enable(name)
        except KeyError:
            raise HTTPException(404, f"no agent called {name!r}")
        return {"name": pack.name, "disabled": False}

    @app.get("/agents/{name}/export", dependencies=[Depends(auth)])
    def export_agent(name: str):
        try:
            raw = core.packs.export(name)
        except KeyError:
            raise HTTPException(404, f"no agent called {name!r}")
        except PackError as e:
            raise HTTPException(422, str(e))
        return Response(content=raw, media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{name}.lantern-agent.json"'})

    @app.post("/agents/{name}/face", dependencies=[Depends(auth)])
    def set_agent_face(name: str, body: AgentFaceIn):
        """Give an agent a face: a short emoji (free), or one drawn icon (approval,
        about $0.01). Blocks until the draw card is approved or declined."""
        try:
            return core.make_agent_face(name, emoji=body.emoji, generate=body.generate)
        except KeyError:
            raise HTTPException(404, f"no agent called {name!r}")
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:  # no key, network: say so plainly
            raise HTTPException(502, f"agent face failed: {type(e).__name__}: {str(e)[:120]}")

    @app.get("/agents/{name}/face", dependencies=[Depends(auth)])
    def agent_face_file(name: str):
        from .agentpacks import PackError
        try:
            path = core.packs.face_path(name)
        except (KeyError, PackError):
            raise HTTPException(404, "no face for that agent")
        return Response(content=path.read_bytes(), media_type="image/webp")

    @app.post("/avatar/design", dependencies=[Depends(auth)])
    def design_avatar(body: AvatarDesignIn):
        """Draw one canonical preview image from words (about $0.01). Accepting it
        renders the full sheet; that step carries the approval."""
        from .studio import StudioError
        try:
            return core.design_avatar(body.brief, body.style)
        except StudioError as e:
            raise HTTPException(400, str(e))
        except Exception as e:  # no key, network: say so plainly
            raise HTTPException(502, f"avatar preview failed: {type(e).__name__}: {str(e)[:120]}")

    @app.post("/avatar/design/{design_id}/accept", dependencies=[Depends(auth)])
    def accept_avatar(design_id: str):
        """Finish a preview into the full sheet (approval, ~$0.06) and set it active.
        A finished design just becomes active again: no model call. Blocks until
        the card is approved or declined, like installing an agent pack."""
        from .avatar import AvatarError
        from .studio import StudioError
        try:
            return core.accept_avatar(design_id)
        except (KeyError, StudioError):
            raise HTTPException(404, "no such design")
        except AvatarError as e:
            raise HTTPException(502, str(e))

    @app.get("/avatar/manifest", dependencies=[Depends(auth)])
    def avatar_manifest():
        return core.studio.manifest()

    @app.get("/avatar/file/{design_id}/{expression}", dependencies=[Depends(auth)])
    def avatar_file(design_id: str, expression: str):
        from .studio import StudioError
        try:
            path = core.studio.file_path(design_id, expression)
        except (KeyError, StudioError):
            raise HTTPException(404, "no such avatar image")
        media = "image/png" if path.suffix == ".png" else "image/webp"
        return Response(content=path.read_bytes(), media_type=media)

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

    @app.websocket("/talk")
    async def talk(ws: WebSocket, token_q: str = Query(default="", alias="token")):
        """S15 live talk: the panel streams microphone PCM here; the realtime transcription comes back as
        {"type": "partial"|"final"|"error"|"done", "text"}. The key stays on this side (talk.py)."""
        if not secrets.compare_digest(token_q, token) or (app.state.phone_mode and not client_is_local(ws)):
            await ws.close(code=4401)   # the desktop's sockets stay on this computer, even with --phone
            return
        await ws.accept()
        from .talk import TalkError, relay
        chunks: asyncio.Queue = asyncio.Queue()

        async def listen_panel() -> None:
            while True:
                m = await ws.receive()
                if m["type"] == "websocket.disconnect":
                    await chunks.put(None)
                    return
                if m.get("bytes"):
                    await chunks.put(m["bytes"])
                elif m.get("text") and '"end"' in m["text"]:
                    await chunks.put(None)

        reader = asyncio.create_task(listen_panel())
        core.emit({"type": "listening", "on": True})
        try:
            out = await relay(chunks.get, ws.send_json, key=os.environ.get(models.API_KEY_ENV, ""))
            if out["audio_ms"]:
                core.memory.log_usage(None, models.VOICE, cost_usd=models.audio_cost(out["audio_ms"]))
            await ws.send_json({"type": "done", "text": out["text"], "audio_ms": out["audio_ms"]})
        except TalkError as e:
            await ws.send_json({"type": "error", "text": str(e)})
        except Exception as e:   # network, a closed socket, a refused handshake: say so, don't crash the server
            try:
                await ws.send_json({"type": "error", "text": f"live talk stopped: {type(e).__name__}: {str(e)[:160]}"})
            except Exception:
                pass
        finally:
            core.emit({"type": "listening", "on": False})
            reader.cancel()
            try:
                await ws.close()
            except Exception:
                pass

    @app.websocket("/events")
    async def events(ws: WebSocket, token_q: str = Query(default="", alias="token")):
        if not secrets.compare_digest(token_q, token) or (app.state.phone_mode and not client_is_local(ws)):
            await ws.close(code=4401)   # the desktop's sockets stay on this computer, even with --phone
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
                                                for t in core.memory.tasks_with_status("interrupted")],
                                "questions": core.questions()})
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

    directory = Path(phone_dir) if phone_dir else PHONE_DIR
    if directory.is_dir():
        app.mount("/m/", StaticFiles(directory=str(directory), html=True), name="phone-static")

    return app


def _put(q: asyncio.Queue, event: dict) -> None:
    if not q.full():
        q.put_nowait(event)


def _jsonable(obj):
    import json
    return json.loads(json.dumps(obj, default=str))


def serve(core: Core, home: Path, host: str = "127.0.0.1", port: int = 8765, hub: ChannelHub | None = None,
            phone: bool = False) -> None:
    if host not in LOCAL_HOSTS and not phone:
        raise SystemExit("Lantern's server only listens on 127.0.0.1")
    if phone:
        host = "0.0.0.0"
    import uvicorn
    token = load_token(home)
    print(f"🏮 Lantern server on http://{host}:{port}  (token in {home / 'token'})")
    if phone:
        print(f"   📱 phone: http://{lan_ip()}:{port}/m  (same Wi-Fi, or your own tunnel for HTTPS)")
    if hub:
        print(f"   channels: {', '.join(sorted(hub.channels)) or 'none'}"
              + ("  ·  WhatsApp webhook: https://<your tunnel>/webhook/whatsapp" if "whatsapp" in hub.channels else ""))
    uvicorn.run(create_app(core, token, hub, phone_mode=phone), host=host, port=port, log_level="warning")
