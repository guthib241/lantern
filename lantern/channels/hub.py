"""The hub between channels (WhatsApp, Telegram, the simulated phone) and Lantern's core.

Incoming, from the owner only (everyone else is ignored and counted):
  text          -> a new task (tagged with the channel), or
  "STOP"        -> the kill switch: cancel the running task, deny every pending approval
  "status"      -> what Lantern is doing
  voice note    -> download -> ffmpeg 16 kHz WAV -> Muse Voice Transcribe (biased toward the
                   names Lantern knows) -> handled like text, with the transcript echoed back
  button tap    -> approve / deny / show an approval. Button ids carry the approval's nonce
                   and the first 8 hex of its payload hash, so only the card that was shown can
                   answer, and only for the action it showed.
Outgoing (core events): every approval request becomes a card with Approve / Deny / Show
buttons on every channel; a finished task's reply goes back to the channel it came from.

Work happens on two small threads (inbox, outbox), so webhooks answer fast and a slow
network never blocks the agent. Outgoing text is scrubbed and audited like any cloud call.
"""
from __future__ import annotations

import datetime as dt
import json
import queue
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

from .. import models
from ..approvals import ApprovalError
from ..scrubber import DEFAULT as SCRUBBER
from ..voice import to_wav16k, transcribe
from .base import Channel, Incoming

STOP = re.compile(r"^\W*/?stop\b", re.I)
STATUS = re.compile(r"^\W*/?status\W*$", re.I)
HELP = re.compile(r"^\W*/?(help|start)\W*$", re.I)
SCOPE = ("I'm Lantern, your computer's task console. Send me a task (\"fill the reimbursement form\", "
         "\"which projects use torch?\"), STOP to halt everything, or status. Approvals arrive here as buttons.")
Transcriber = Callable[..., dict]   # (wav_path, keywords=[...]) -> {"transcript", "audioDurationMs", ...}


class ChannelHub:
    def __init__(self, core: Any, channels: list[Channel], *, transcriber: Transcriber | None = None,
                 converter: Callable[[Path, Path], Path] | None = None, media_dir: str | Path | None = None):
        self.core = core
        self.channels = {c.name: c for c in channels}
        self.transcriber = transcriber or transcribe
        self.converter = converter or to_wav16k
        self.media_dir = Path(media_dir or core.cfg.media_dir).expanduser()
        self.origin: dict[int, str] = {}          # task id -> channel that asked for it
        self.ignored = 0                          # messages from anyone but the owner
        self.errors: deque = deque(maxlen=50)
        self._seen: deque = deque(maxlen=500)     # message ids already handled (webhooks retry)
        self._inbox: queue.Queue = queue.Queue()
        self._outbox: queue.Queue = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._unsubscribe: Callable[[], None] | None = None

    # ---- lifecycle -------------------------------------------------------------------
    def start(self, listen: bool = True) -> "ChannelHub":
        """Start the workers; `listen` also starts polling channels (tests drive them by hand)."""
        self._unsubscribe = self.core.subscribe(self._on_event)
        for name, fn in (("lantern-inbox", self._inbox_loop), ("lantern-outbox", self._outbox_loop)):
            t = threading.Thread(target=fn, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        if listen:
            for c in self.channels.values():
                c.start(self.receive)
        return self

    def stop(self) -> None:
        for c in self.channels.values():
            c.stop()
        if self._unsubscribe:
            self._unsubscribe()
        self._inbox.put(None)
        self._outbox.put(None)

    def receive(self, incoming: Incoming) -> None:
        """Called by webhooks and pollers; returns immediately."""
        self._inbox.put(incoming)

    def wait_idle(self, timeout: float = 10.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if self._inbox.unfinished_tasks == 0 and self._outbox.unfinished_tasks == 0:
                return True
            time.sleep(0.01)
        return False

    # ---- workers ---------------------------------------------------------------------
    def _inbox_loop(self) -> None:
        while True:
            item = self._inbox.get()
            try:
                if item is None:
                    return
                self.handle(item)
            except Exception as e:  # one bad message must not kill the channel
                self._error("inbox", e)
            finally:
                self._inbox.task_done()

    def _outbox_loop(self) -> None:
        while True:
            item = self._outbox.get()
            try:
                if item is None:
                    return
                name, kind, args = item
                targets = [self.channels[name]] if name else list(self.channels.values())
                for ch in targets:
                    try:
                        self._deliver(ch, kind, args)
                    except Exception as e:
                        self._error(ch.name, e)
            finally:
                self._outbox.task_done()

    def _error(self, where: str, e: Exception) -> None:
        msg = f"{where}: {type(e).__name__}: {str(e)[:200]}"
        self.errors.append(msg)
        self.core.emit({"type": "channel_error", "error": msg})

    def _deliver(self, ch: Channel, kind: str, args: tuple) -> None:
        text = SCRUBBER.scrub(args[0]).text
        if kind == "text":
            ch.send_text(text)
        else:
            ch.send_buttons(text, args[1])
        destination = getattr(ch, "destination", None)
        if destination:  # what left the laptop through this channel
            body = json.dumps({"text": text, "buttons": args[1] if kind == "buttons" else []}).encode()
            self.core.audit.record(lane="cloud", destination=destination, endpoint=f"{ch.name}.send",
                                   model=ch.name, payload=body, preview=text)

    def say(self, channel: str | None, text: str) -> None:
        self._outbox.put((channel, "text", (text,)))

    # ---- incoming ----------------------------------------------------------------------
    def handle(self, inc: Incoming) -> None:
        ch = self.channels.get(inc.channel)
        if ch is None or not ch.is_owner(inc.sender):
            self.ignored += 1
            return
        if inc.msg_id:
            key = (inc.channel, inc.msg_id)
            if key in self._seen:
                return
            self._seen.append(key)
        if inc.kind == "button":
            return self._button(ch, inc)
        text = inc.text
        if inc.kind == "voice":
            text = self._voice(ch, inc)
            if text is None:
                return
        self._text(ch, text)

    def _text(self, ch: Channel, text: str) -> None:
        t = (text or "").strip()
        if not t:
            return
        if STOP.match(t):
            r = self.core.stop()
            n = r["denied_approvals"]
            self.say(ch.name, "🛑 Stopped. " + ("I cancelled the running task" if r.get("task_id") else "Nothing was running")
                     + (f" and denied {n} pending approval{'s' if n != 1 else ''}." if n else "."))
            return
        if STATUS.match(t):
            return self.say(ch.name, self._status())
        if HELP.match(t):
            return self.say(ch.name, SCOPE)
        task_id = self.core.submit(f"[{ch.name}] {t}")
        self.origin[task_id] = ch.name
        self.say(ch.name, f"🏮 On it (task #{task_id}).")

    def _status(self) -> str:
        cur = self.core.current_task
        lines = [f"Working on task #{cur}: {self.core.memory.get_task(cur).goal}" if cur else "Idle."]
        pending = self.core.bus.pending()
        if pending:
            lines.append(f"{len(pending)} approval(s) waiting: " + "; ".join(a.summary[:60] for a in pending))
        for t in self.core.memory.recent_tasks(3):
            if t.status != "active":
                lines.append(f"#{t.id} {t.status}: {(t.summary or t.goal)[:80]}")
        return "\n".join(lines)

    def _button(self, ch: Channel, inc: Incoming) -> None:
        parts = inc.button.split(":")
        verb, aid = parts[0], parts[1] if len(parts) > 1 else ""
        try:
            a = self.core.bus.get(aid)
            if verb == "show":
                ch.acknowledge(inc, "Details sent")
                self.say(ch.name, self._details(a))
                return
            if verb not in ("approve", "deny") or len(parts) != 4:
                raise ApprovalError("unknown button")
            nonce, short_hash = parts[2], parts[3]
            if not a.payload_hash.startswith(short_hash):
                raise ApprovalError("the action changed since this card was sent; ask again")
            self.core.bus.decide(aid, nonce, verb == "approve", by=ch.name)
        except ApprovalError as e:
            ch.acknowledge(inc, "Not open any more")
            self.say(ch.name, f"That approval isn't open any more ({e}).")
            return
        ch.acknowledge(inc, "Approved" if verb == "approve" else "Denied")
        mark = "✅ Approved" if verb == "approve" else "❌ Denied"
        self.say(ch.name, f"{mark}: {a.summary[:120]}")

    @staticmethod
    def _details(a) -> str:
        payload = json.dumps(a.payload, indent=1, ensure_ascii=False, default=str)
        return (f"🔎 {a.action} (#{a.id})\n{a.summary}\n\nExactly this will run (hashes, not contents):\n"
                f"{payload[:2500]}\n\npayload sha256 {a.payload_hash[:16]}…")

    def _voice(self, ch: Channel, inc: Incoming) -> str | None:
        try:
            data, ext = inc.fetch_audio()
            self.media_dir.mkdir(parents=True, exist_ok=True)
            stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")  # short digit runs: never card-shaped
            src = self.media_dir / f"{ch.name}-voice-{stamp}{ext}"
            src.write_bytes(data)
            wav = self.converter(src, src.with_suffix(".wav"))
            self.core.audit.record(lane="cloud", destination=getattr(self.core.cfg, "base_url", models.BASE_URL),
                                   endpoint="asr.transcribe",
                                   model=models.VOICE, payload=Path(wav).read_bytes(), preview="[voice note]")
            result = self.transcriber(wav, keywords=self.keywords())
        except Exception as e:  # download, ffmpeg (CalledProcessError), transcription: all end the same way
            self._error(f"{ch.name} voice", e)
            self.say(ch.name, f"I couldn't hear that voice note ({type(e).__name__}). Try again or type it.")
            return None
        text = str(result.get("transcript", "")).strip()
        ms = int(result.get("audioDurationMs") or 0)
        cost = models.audio_cost(ms)
        self.core.memory.log_usage(None, models.VOICE, cost_usd=cost)
        self.core.emit({"type": "usage", "model": models.VOICE, "badge": models.badge(models.VOICE),
                        "audio_ms": ms, "cost_usd": cost})
        if not text:
            self.say(ch.name, "I got the voice note but couldn't make out any words.")
            return None
        self.say(ch.name, f"🎙️ “{text}”")
        return text

    def keywords(self) -> list[str]:
        """Names to bias transcription toward: short remembered facts, saved form names, profile names."""
        words = ["Lantern"]
        for key, value in self.core.memory.facts().items():
            for v in (key, value):
                if 0 < len(v) <= 40 and len(v.split()) <= 4 and "@" not in v and "/" not in v:
                    words.append(v)
        forms = getattr(self.core, "forms", None)
        if forms is not None:
            data = forms.vault.load()
            words += [k for k in data["forms"]]
            words += [v for k, v in data["profile"].items() if "name" in k and len(v) <= 40]
        seen, out = set(), []
        for w in words:
            if w.lower() not in seen:
                seen.add(w.lower())
                out.append(w)
        return out[:100]

    # ---- outgoing (core events) ----------------------------------------------------------
    def _on_event(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "approval" and event.get("event") == "requested":
            a = event["approval"]
            h8 = a["payload_hash"][:8]
            minutes = max(1, round((a["expires_at"] - time.time()) / 60))
            text = f"🏮 Lantern needs your OK\n\n{a['summary']}\n\n⏳ expires in {minutes} min · #{a['id']}"
            buttons = [(f"approve:{a['id']}:{a['nonce']}:{h8}", "✅ Approve"),
                       (f"deny:{a['id']}:{a['nonce']}:{h8}", "❌ Deny"),
                       (f"show:{a['id']}", "🔎 Show")]
            self._outbox.put((None, "buttons", (text, buttons)))
        elif kind == "done":
            name = self.origin.pop(event.get("id"), None)
            if name and event.get("status") != "cancelled":  # STOP already got its own reply
                reply = (event.get("reply") or event.get("summary") or "").strip()
                status = event.get("status")
                if status == "failed" and not reply:
                    reply = f"That didn't work: {event.get('summary', '')}"
                if reply:
                    self.say(name, reply)
