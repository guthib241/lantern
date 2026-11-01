"""The hub between channels (WhatsApp, Telegram, the simulated phone) and Lantern's core.

Incoming, from the owner only (everyone else is ignored and counted):
  text          -> a new task (tagged with the channel), or
  "STOP"        -> the kill switch: cancel the running task, deny every pending approval
  "status"      -> what Lantern is doing
  "resume"      -> run the tasks cut short when Lantern stopped
  "undo"        -> put back the files the last task moved (one approval, no model call)
  "loops"       -> scheduled agents and whether each loop is on; "pause <name>" turns one off
  "agents"      -> installed agents that run on the phone, with Run buttons
  "run <name> key=value ..." -> start a phone agent (a laptop-only agent is refused)
  "receipt [N]" -> the last finished task's receipt (or task N's): changes, checks, approvals, data sent, cost
  voice note    -> download -> ffmpeg 16 kHz WAV -> Muse Voice Transcribe (biased toward the
                   names Lantern knows) -> handled like text, with the transcript echoed back
  button tap    -> approve / deny / show an approval. Button ids carry the approval's nonce
                   and the first 8 hex of its payload hash, so only the card that was shown can
                   answer, and only for the action it showed. "run:<name>" starts a phone agent.
Outgoing (core events): an approval request becomes a card with Approve / Deny / Show
buttons on the phone channels, unless its task is a laptop-only agent (the panel
shows those); a finished task's reply goes back to the channel it came from, and a
scheduled agent run with no channel reports to its pack's surface.

Work happens on two small threads (inbox, outbox), so webhooks answer fast and a slow
network never blocks the agent. Outgoing text is scrubbed and audited like any cloud call.
"""
from __future__ import annotations

import datetime as dt
import json
import queue
import re
import shlex
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

from .. import models
from ..approvals import ApprovalError
from ..linkopen import clean_url
from ..scrubber import DEFAULT as SCRUBBER
from ..undo import WORDS as UNDO_WORDS, UndoError
from ..voice import to_wav16k, transcribe
from .base import Channel, Incoming

STOP = re.compile(r"^\W*/?stop\b", re.I)
STATUS = re.compile(r"^\W*/?status\W*$", re.I)
HELP = re.compile(r"^\W*/?(help|start)\W*$", re.I)
RESUME = re.compile(r"^\W*/?(resume|continue)\W*$", re.I)
RECEIPT = re.compile(r"^\W*/?receipt(?:\s+(?:for\s+)?(?:task\s+)?#?(\d+))?\W*$", re.I)
LOOPS = re.compile(r"^\W*/?loops\W*$", re.I)
PAUSE = re.compile(r"^\W*/?pause\s+([A-Za-z0-9][A-Za-z0-9-]*)\W*$", re.I)
AGENTS = re.compile(r"^\W*/?agents\W*$", re.I)
RUN = re.compile(r"^\W*/?run\s+([A-Za-z0-9][A-Za-z0-9-]*)", re.I)
LINK_OPEN = re.compile(r"^\W*(?:open\s+this\s+on\s+my\s+laptop\s+|open\s+)?(https?://\S+)\W*$", re.I)
LAPTOP_ONLY = "this agent runs on the laptop only; change it in the Agents tab"
SCOPE = ("I'm Lantern, your computer's task console. Send me a task (\"fill the reimbursement form\", "
         "\"which projects use torch?\", \"remind me at 6 to call Priya\"), STOP to halt everything, status, "
         "resume, undo (puts back the files the last task moved), or receipt (proof of what the last task did). "
         "Agents that run on the phone: send agents to list them, run <name> key=value to start one. "
         "Approvals arrive here as buttons.")
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
        self.pack_of: dict[int, str] = {}         # task id -> agent pack it runs (for surface routing)
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
        cut = getattr(self.core, "interrupted", [])
        if cut:
            names = "; ".join(f"#{t.id} {t.goal.split(chr(10))[0][:60]}" for t in cut[:3])
            self.say(None, f"🏮 I was stopped in the middle of {len(cut)} task{'s' if len(cut) != 1 else ''}: "
                           f"{names}{' …' if len(cut) > 3 else ''}. Reply resume to continue.")
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
        if RESUME.match(t):
            new_ids = self.core.resume()
            for task_id in new_ids:
                self.origin[task_id] = ch.name
            return self.say(ch.name, f"🏮 Resuming {len(new_ids)} task{'s' if len(new_ids) != 1 else ''} "
                                     f"(#{', #'.join(map(str, new_ids))})." if new_ids else "Nothing to resume.")
        if m := RECEIPT.match(t):
            return self.say(ch.name, self._receipt(int(m.group(1)) if m.group(1) else None))
        if LOOPS.match(t):
            return self.say(ch.name, self._loops())
        if m := PAUSE.match(t):
            return self.say(ch.name, self._pause(m.group(1)))
        if AGENTS.match(t):
            return self._agents(ch)
        if m := RUN.match(t):
            if self._run_agent(ch, m.group(1), t[m.end():]):
                return
        if UNDO_WORDS.match(t):
            try:
                task_id = self.core.undo_last()
            except UndoError as e:
                return self.say(ch.name, f"Nothing to undo: {e}.")
            self.origin[task_id] = ch.name
            return self.say(ch.name, f"↩️ Undo is task #{task_id}. Approve the card to put the files back.")
        if m := LINK_OPEN.match(t):  # phone fast path: no model call, faster and free
            url = clean_url(m.group(1))
            if url is not None:
                task_id = self.core.submit(f"[{ch.name}] open this link on my laptop: {url}")
                self.origin[task_id] = ch.name
                return self.say(ch.name, f"🏮 On it (task #{task_id}).")
        task_id = self.core.submit(f"[{ch.name}] {t}")
        self.origin[task_id] = ch.name
        self.say(ch.name, f"🏮 On it (task #{task_id}).")

    def _loops(self) -> str:
        """Every scheduled pack and whether its loop is on."""
        from ..reminders import describe
        loops = self.core.loops()
        if not loops:
            return "No scheduled agents. Turn one on in the Agents tab."
        lines = []
        for loop in loops:
            state = "on" if loop["enabled"] else "off"
            extra = f", next run {describe(loop['next_run'])}" if loop["enabled"] and loop["next_run"] else ""
            last = f", last run {loop['last_status']}" if loop.get("last_status") else ""
            lines.append(f"{loop['name']} ({loop.get('description') or 'scheduled'}): {state}{extra}{last}")
        lines.append("Reply 'pause <name>' to turn one off.")
        return "\n".join(lines)

    def _pause(self, name: str) -> str:
        try:
            self.core.loop_pause(name)
        except KeyError:
            return f"No loop called '{name}': no agent by that name is installed."
        except Exception as e:   # a tampered file refuses instead of pausing
            return f"Couldn't pause '{name}': {e}."
        return f"Paused the '{name}' loop."

    # ---- agent packs on the phone (T3d) -------------------------------------------
    def _pack_surface(self, name: str) -> str:
        """This pack's Runs-on setting. An unreadable store entry stays on the laptop."""
        try:
            return self.core.packs.surface(name)
        except Exception:
            return "laptop"

    def _phone_packs(self) -> list[dict]:
        """Installed, enabled packs whose surface includes the phone."""
        out = []
        for entry in self.core.packs.list():
            if entry.get("disabled"):
                continue
            surface = entry.get("surface") or self._pack_surface(entry["name"])
            if surface in ("phone", "both"):
                out.append(entry)
        return out

    def _agents(self, ch: Channel) -> None:
        """List the phone agents in chunks of 3 (the inline-button limit), each with a Run button."""
        packs = self._phone_packs()
        if not packs:
            self.say(ch.name, "No agents run on the phone yet. Switch one on in the Agents tab (Runs on: Phone or Both).")
            return
        for i in range(0, len(packs), 3):
            chunk = packs[i:i + 3]
            lines = [f"📦 Agents on this phone ({len(packs)}): reply run <name> key=value, or tap Run."]
            buttons = []
            for entry in chunk:
                lines.append(f"• {entry.get('title', entry['name'])} ({entry['name']}) — "
                             f"{entry.get('description', '')}".rstrip(" —").rstrip())
                buttons.append((f"run:{entry['name']}", f"Run {entry['name']}"[:20]))
            self._outbox.put((ch.name, "buttons", ("\n".join(lines), buttons)))

    @staticmethod
    def _run_args(rest: str) -> dict:
        """`folder=Notes who=Priya` (quotes for spaces) -> {folder: Notes, ...}."""
        try:
            tokens = shlex.split(rest or "")
        except ValueError as e:
            raise ValueError(f"couldn't read that ({e}); use key=value, e.g. run NAME folder=Downloads") from None
        args: dict[str, str] = {}
        for token in tokens:
            if "=" not in token:
                raise ValueError(f"'{token}' is not key=value; e.g. run NAME folder=Downloads")
            key, value = token.split("=", 1)
            if not key.strip():
                raise ValueError(f"'{token}' is not key=value; e.g. run NAME folder=Downloads")
            args[key.strip()] = value.strip()
        return args

    def _run_agent(self, ch: Channel, name: str, rest: str) -> bool:
        """Start an installed pack from the phone. True when handled; False when
        `name` is no agent at all (the text stays an ordinary task)."""
        try:
            pack = self.core.packs.get(name)   # re-validated: a changed file is refused, not run
        except KeyError:
            return False
        except Exception as e:   # PackError, ValueError, OSError: say so, don't run
            self.say(ch.name, f"Couldn't run '{name}': {e}.")
            return True
        if pack.disabled:
            self.say(ch.name, f"Agent '{name}' is off (disabled). Turn it on in the Agents tab to run it.")
            return True
        if self._pack_surface(name) == "laptop":
            self.say(ch.name, f"Agent '{name}': {LAPTOP_ONLY}.")
            return True
        try:
            args = self._run_args(rest)
        except ValueError as e:
            self.say(ch.name, f"{e}.")
            return True
        missing = [i for i in pack.inputs if i.key not in args]
        if missing:
            lines = [f"To run '{pack.title}' I still need:"]
            lines += [f"– {i.question} ({i.key}" + (f", e.g. {i.example}" if i.example else "") + ")"
                      for i in missing]
            lines.append(f"Reply: run {name} " + " ".join(f"{i.key}=…" for i in pack.inputs))
            self.say(ch.name, "\n".join(lines))
            return True
        try:
            task_id = self.core.submit("", pack=name, inputs=args)
        except ValueError as e:   # an unknown key, a value too long: the store's own check
            self.say(ch.name, f"Couldn't run '{name}': {e}.")
            return True
        self.origin[task_id] = ch.name
        self.say(ch.name, f"🏮 On it (task #{task_id}).")
        return True

    def _receipt(self, task_id: int | None) -> str:
        from ..receipt import as_text, build
        if task_id is None:
            done = [t for t in self.core.memory.recent_tasks(10) if t.status not in ("active",)]
            if not done:
                return "No finished tasks yet, so no receipt."
            task_id = done[0].id
        try:
            return as_text(build(self.core.memory, self.core.audit, task_id))
        except KeyError:
            return f"There's no task #{task_id}."

    def _status(self) -> str:
        cur = self.core.current_task
        lines = [f"Working on task #{cur}: {self.core.memory.get_task(cur).goal}" if cur else "Idle."]
        pending = self.core.bus.pending()
        if pending:
            lines.append(f"{len(pending)} approval(s) waiting: " + "; ".join(a.summary[:60] for a in pending))
        upcoming = self.core.memory.reminders()[:3]
        if upcoming:
            lines.append("Reminders: " + "; ".join(f"{r.public()['due']} {r.text[:40]}" for r in upcoming))
        for t in self.core.memory.recent_tasks(3):
            if t.status != "active":
                lines.append(f"#{t.id} {t.status}: {(t.summary or t.goal)[:80]}")
        return "\n".join(lines)

    def _button(self, ch: Channel, inc: Incoming) -> None:
        parts = inc.button.split(":")
        verb, aid = parts[0], parts[1] if len(parts) > 1 else ""
        if verb == "run" and len(parts) == 2:
            ch.acknowledge(inc, "Starting")
            if not self._run_agent(ch, parts[1], ""):
                self.say(ch.name, f"No agent called '{parts[1]}'. Send agents to see which run on the phone.")
            return
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
            wav = self.converter(src, src.with_name(src.stem + ".16k.wav"))   # never onto itself (.wav in)
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
    def _surface_muted(self, task_id: int | None) -> bool:
        """A laptop-only agent task: the panel shows its cards and results, the phone stays quiet."""
        pack = self.pack_of.get(task_id) if task_id is not None else None
        return pack is not None and self._pack_surface(pack) == "laptop"

    def _on_event(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "queued" and event.get("task_id") is not None:
            if event.get("pack"):   # a pack run: remember it, so cards and results follow its surface
                if len(self.pack_of) > 1000:
                    self.pack_of.pop(next(iter(self.pack_of)))
                self.pack_of[event["task_id"]] = event["pack"]
            return
        if kind == "approval" and event.get("event") == "requested":
            if self._surface_muted(event.get("task_id")):
                return
            a = event["approval"]
            h8 = a["payload_hash"][:8]
            minutes = max(1, round((a["expires_at"] - time.time()) / 60))
            text = f"🏮 Lantern needs your OK\n\n{a['summary']}\n\n⏳ expires in {minutes} min · #{a['id']}"
            buttons = [(f"approve:{a['id']}:{a['nonce']}:{h8}", "✅ Approve"),
                       (f"deny:{a['id']}:{a['nonce']}:{h8}", "❌ Deny"),
                       (f"show:{a['id']}", "🔎 Show")]
            self._outbox.put((None, "buttons", (text, buttons)))
        elif kind == "reminder":
            note = " (it was due while Lantern was off)" if event.get("late") else ""
            self._outbox.put((None, "text", (f"⏰ Reminder: {event.get('text', '')}{note}",)))
        elif kind == "loop" and event.get("event") == "paused":
            if event.get("name") and self._pack_surface(event["name"]) == "laptop":
                return   # a laptop-only loop pauses quietly; the panel shows it
            self._outbox.put((None, "text", (f"⏸️ {event.get('text', 'A loop paused itself.')}",)))
        elif kind == "done":
            task_id = event.get("id")
            name = self.origin.pop(task_id, None)
            pack = self.pack_of.pop(task_id, None)
            if pack is not None and self._pack_surface(pack) == "laptop":
                return   # a laptop-only run reports to the panel, never the phone
            if event.get("status") == "cancelled":  # STOP already got its own reply
                return
            reply = (event.get("reply") or event.get("summary") or "").strip()
            status = event.get("status")
            if status == "failed" and not reply:
                reply = f"That didn't work: {event.get('summary', '')}"
            if not reply:
                return
            if name:
                self.say(name, reply)
            elif pack is not None:
                # A scheduled run has no channel: it reports to its pack's surface.
                self._outbox.put((None, "text", (reply,)))
