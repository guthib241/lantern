"""The hub between channels (WhatsApp, Telegram, the simulated phone) and Lantern's core.

Incoming, from the owner only (everyone else is ignored and counted):
  text          -> a new task (tagged with the channel), or
  "STOP"        -> the kill switch: cancel the running task, deny every pending approval
  "status"      -> what Lantern is doing
  "resume"      -> run the tasks cut short when Lantern stopped
  "undo"        -> put back the files the last task moved (one approval, no model call)
  "receipt [N]" -> the last finished task's receipt (or task N's): changes, checks, approvals, data sent, cost
  "#N answer"   -> the answer to task N's question (ask_user; the task was parked, scheduler.py), also by button
  "learn [N]"   -> compile the last finished task (or task N) into a routine that runs with no model calls (S4)
  voice note    -> download -> ffmpeg 16 kHz WAV -> Muse Voice Transcribe (biased toward the
                   names Lantern knows) -> handled like text, with the transcript echoed back
  button tap    -> approve / deny / show an approval. Button ids carry the approval's nonce
                   and the first 8 hex of its payload hash, so only the card that was shown can
                   answer, and only for the action it showed.
Outgoing (core events): every approval request becomes a card with Approve / Deny / Show
buttons on every channel; a finished task's reply goes back to the channel it came from.
Questions from parked tasks that arrive within a second of each other go out as one message.

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
from ..undo import WORDS as UNDO_WORDS, UndoError
from ..voice import to_wav16k, transcribe
from .base import Channel, Incoming

STOP = re.compile(r"^\W*/?stop\b", re.I)
STATUS = re.compile(r"^\W*/?status\W*$", re.I)
HELP = re.compile(r"^\W*/?(help|start)\W*$", re.I)
RESUME = re.compile(r"^\W*/?(resume|continue)\W*$", re.I)
RECEIPT = re.compile(r"^\W*/?receipt(?:\s+(?:for\s+)?(?:task\s+)?#?(\d+))?\W*$", re.I)
ANSWER = re.compile(r"^\s*#(\d+)[\s:,.-]+(.+)$", re.S)   # "#12 residential": an answer to task 12's question
LEARN = re.compile(r"^\W*/?learn(?:\s+(?:it|this|that|task))?(?:\s+#?(\d+))?\W*$", re.I)
QUESTION_DELAY = 1.0   # seconds: questions that arrive together go out as one message
SCOPE = ("I'm Lantern, your computer's task console. Send me a task (\"fill the reimbursement form\", "
         "\"which projects use torch?\", \"remind me at 6 to call Priya\"), STOP to halt everything, status, "
         "resume, undo (puts back the files the last task moved), receipt (proof of what the last task did), or "
         "learn (turn the last task into a routine that runs with no model calls). Send a photo of a paper (from "
         "your phone or glasses) and say what it's for: I scan it and run the routine with the scan attached. Send a "
         "how-to video and I'll send back its steps to check before I do them. "
         "Approvals arrive here as buttons.")
Transcriber = Callable[..., dict]   # (wav_path, keywords=[...]) -> {"transcript", "audioDurationMs", ...}


PHOTO_WAIT = 15.0


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
        self.question_delay = QUESTION_DELAY
        self._asked: set[int] = set()             # tasks whose open question was already sent
        self._question_timer: threading.Timer | None = None
        self._question_lock = threading.Lock()
        self._finished: dict[int, str] = {}       # recently finished task -> its channel (for learn offers)
        self._offers: deque = deque(maxlen=20)   # tasks Lantern offered to learn, newest last
        self.photo_wait = PHOTO_WAIT              # S9: seconds to wait for words after a photo without a caption
        self._photos: dict[str, tuple[bytes, int]] = {}   # channel -> (photo waiting for words, its number)
        self._photo_n = 0
        self._photo_lock = threading.Lock()

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
        if hasattr(self.core, "questions") and self.core.questions():   # asked before a restart: ask again
            self._schedule_questions()
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
        if inc.kind == "photo":
            return self._photo(ch, inc)
        if inc.kind == "video":
            return self._video(ch, inc)
        text = inc.text
        if inc.kind == "voice":
            text = self._voice(ch, inc)
            if text is None:
                return
        if not STOP.match((text or "").strip()) and (photo := self._take_photo(ch.name)) is not None:
            return self._start_photo(ch, photo, text)   # the words right after a photo say what it's for
        self._text(ch, text)

    # ---- photos (S9: from the glasses, or any phone) ------------------------------------------------
    def _photo(self, ch: Channel, inc: Incoming) -> None:
        try:
            image, _ext = inc.fetch_image()
        except Exception as e:   # download failed
            self._error(f"{ch.name} photo", e)
            return self.say(ch.name, f"I couldn't get that photo ({type(e).__name__}). Try sending it again.")
        if (inc.text or "").strip():
            return self._start_photo(ch, image, inc.text)
        with self._photo_lock:
            self._photo_n += 1
            n = self._photo_n
            self._photos[ch.name] = (image, n)
        timer = threading.Timer(self.photo_wait, self._photo_waited, (ch, n))
        timer.daemon = True
        timer.start()
        self.say(ch.name, f"📷 Got the photo. Tell me what it's for (a message or a voice note), or I'll read it in "
                          f"{self.photo_wait:.0f} s.")

    def _video(self, ch: Channel, inc: Incoming) -> None:
        """S12: a how-to video: Lantern works out the steps and sends them back as one card to approve."""
        try:
            video, _ext = inc.fetch_video()
            task_id = self.core.reel(video, words=inc.text, transcriber=lambda wav: self.transcriber(wav))
        except ValueError as e:
            return self.say(ch.name, f"I can't follow that video: {e}.")
        except Exception as e:   # download failed
            self._error(f"{ch.name} video", e)
            return self.say(ch.name, f"I couldn't get that video ({type(e).__name__}). Try sending it again.")
        self.origin[task_id] = ch.name
        self.say(ch.name, f"🎬 Watching it (task #{task_id}). I'll send you the steps to check before I do anything.")

    def _take_photo(self, channel: str) -> bytes | None:
        with self._photo_lock:
            item = self._photos.pop(channel, None)
        return item[0] if item else None

    def _photo_waited(self, ch: Channel, n: int) -> None:
        with self._photo_lock:
            item = self._photos.get(ch.name)
            if item is None or item[1] != n:
                return
            del self._photos[ch.name]
        self._start_photo(ch, item[0], "")

    def _start_photo(self, ch: Channel, image: bytes, words: str) -> None:
        try:
            task_id = self.core.photo(image, words)
        except ValueError as e:
            return self.say(ch.name, f"I can't scan photos right now: {e}.")
        self.origin[task_id] = ch.name
        self.say(ch.name, f"📷 Scanning it (task #{task_id}).")

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
        if self._answer(ch, t):
            return
        if m := LEARN.match(t):
            return self._learn(ch, int(m.group(1)) if m.group(1) else None)
        if UNDO_WORDS.match(t):
            try:
                task_id = self.core.undo_last()
            except UndoError as e:
                return self.say(ch.name, f"Nothing to undo: {e}.")
            self.origin[task_id] = ch.name
            return self.say(ch.name, f"↩️ Undo is task #{task_id}. Approve the card to put the files back.")
        task_id = self.core.submit(f"[{ch.name}] {t}")
        self.origin[task_id] = ch.name
        self.say(ch.name, f"🏮 On it (task #{task_id}).")

    def _learn(self, ch: Channel, task_id: int | None) -> None:
        if task_id is None:
            task_id = self._offers[-1] if self._offers else next(
                (t.id for t in self.core.memory.recent_tasks(10) if t.status == "done"), None)
        if task_id is None:
            return self.say(ch.name, "There's no finished task to learn yet.")
        try:
            job = self.core.learn(task_id)
        except (ValueError, KeyError) as e:
            return self.say(ch.name, f"Can't learn that one: {str(e).strip(chr(39))}.")
        self.origin[job] = ch.name
        self.say(ch.name, f"🧠 Learning task #{task_id} as a routine (task #{job}).")

    def _answer(self, ch: Channel, text: str) -> bool:
        """'#12 residential', or just an option of the only open question. True if it was an answer."""
        questions = self.core.questions() if hasattr(self.core, "questions") else []
        m = ANSWER.match(text)
        if m:
            task_id, answer = int(m.group(1)), m.group(2).strip()
        elif len(questions) == 1 and any(text.lower() == o.lower() for o in questions[0]["options"]):
            task_id, answer = questions[0]["task_id"], text
        else:
            return False
        try:
            self.core.answer(task_id, answer, by=ch.name)
        except ValueError as e:
            if m and not any(q["task_id"] == task_id for q in questions):
                return False   # "#3 is great" with no question open for #3: an ordinary message
            self.say(ch.name, f"Couldn't use that answer: {e}.")
            return True
        self.origin.setdefault(task_id, ch.name)
        self.say(ch.name, f"✅ Thanks. Task #{task_id} continues.")
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
        sched = getattr(self.core, "scheduler", None)
        if sched is not None:
            snap = sched.snapshot()
            if snap["running"] or snap["queued"] or snap["parked"]:
                lines[0] = (f"Running {len(snap['running'])}, queued {len(snap['queued'])}, "
                            f"waiting {len(snap['parked'])} (of {snap['workers']} at once).")
            for q in self.core.questions():
                lines.append(f"#{q['task_id']} asks: {q['question'][:100]}")
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
        if verb == "answer":
            return self._answer_button(ch, inc, parts)
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

    def _answer_button(self, ch: Channel, inc: Incoming, parts: list[str]) -> None:
        """answer:<task>:<option index>, from a question card."""
        try:
            task_id, index = int(parts[1]), int(parts[2])
            [q] = [q for q in self.core.questions() if q["task_id"] == task_id]
            self.core.answer(task_id, q["options"][index], by=ch.name)
        except (ValueError, IndexError):
            ch.acknowledge(inc, "Not open any more")
            self.say(ch.name, "That question isn't open any more.")
            return
        ch.acknowledge(inc, "Answered")
        self.origin.setdefault(task_id, ch.name)
        self.say(ch.name, f"✅ {q['options'][index][:60]}: task #{task_id} continues.")

    # ---- questions from parked tasks, batched -------------------------------------------
    def _schedule_questions(self) -> None:
        with self._question_lock:
            if self._question_timer is None:
                self._question_timer = threading.Timer(self.question_delay, self._send_questions)
                self._question_timer.daemon = True
                self._question_timer.start()

    def _send_questions(self) -> None:
        with self._question_lock:
            self._question_timer = None
            open_now = self.core.questions()
            new = [q for q in open_now if q["task_id"] not in self._asked]
            self._asked = {q["task_id"] for q in open_now}
        if new:
            self._outbox.put(question_message(new))

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
        elif kind == "learn_offer":
            task_id = event.get("task_id")
            self._offers.append(task_id)
            name = self._finished.get(task_id)
            if event.get("auto"):
                self._outbox.put((name, "text", (f"🧠 You've asked for this before, so I'm learning task #{task_id} "
                                                  "as a routine: next time it runs with no model calls.",)))
            elif name:
                self.say(name, f"💡 Want me to learn this? Next time it would run with no model calls. "
                               f"Reply: learn {task_id}")
        elif kind == "queued" and (m := re.match(r"Learn task #(\d+) ", str(event.get("goal", "")))):
            if (name := self._finished.get(int(m.group(1)))) and event.get("task_id") not in self.origin:
                self.origin[event["task_id"]] = name   # an automatic learn reports back where the task came from
        elif kind == "questions":
            if {q["task_id"] for q in event.get("items", [])} - self._asked:
                self._schedule_questions()
            else:   # answered or withdrawn: forget them, so a new question for the same task is sent
                with self._question_lock:
                    self._asked &= {q["task_id"] for q in event.get("items", [])}
        elif kind == "digest" and event.get("text"):   # S11: the morning digest after the nightly check
            self._outbox.put((None, "text", (event["text"],)))
        elif kind == "reminder":
            note = " (it was due while Lantern was off)" if event.get("late") else ""
            self._outbox.put((None, "text", (f"⏰ Reminder: {event.get('text', '')}{note}",)))
        elif kind == "done":
            name = self.origin.pop(event.get("id"), None)
            if name:
                self._finished[event.get("id")] = name
                while len(self._finished) > 50:
                    self._finished.pop(next(iter(self._finished)))
            if name and event.get("status") != "cancelled":  # STOP already got its own reply
                # S0: a reply that quotes private content goes out with that part kept on the computer
                reply = (event.get("reply_public") or event.get("reply") or event.get("summary") or "").strip()
                status = event.get("status")
                if status == "failed" and not reply:
                    reply = f"That didn't work: {event.get('summary', '')}"
                if reply:
                    self.say(name, reply)


def question_message(questions: list[dict]) -> tuple:
    """One outbox item for one or more open questions. A single question with up to 3 short options gets buttons;
    otherwise people reply "#<task> <answer>"."""
    def warn(q: dict) -> str:
        return " (asked after reading untrusted content)" if q.get("tainted") else ""

    if len(questions) == 1:
        q = questions[0]
        text = f"🏮 Task #{q['task_id']} needs an answer{warn(q)}:\n\n{q['question']}\n\n({q['goal'][:80]})"
        opts = q["options"]
        if 0 < len(opts) <= 3:
            return (None, "buttons", (text, [(f"answer:{q['task_id']}:{i}", o[:20]) for i, o in enumerate(opts)]))
        if opts:
            text += "\nOptions: " + " / ".join(opts)
        return (None, "text", (text + f"\n\nReply: #{q['task_id']} your answer",))
    lines = [f"🏮 {len(questions)} tasks need an answer:"]
    for q in questions:
        opts = f" ({' / '.join(q['options'])})" if q["options"] else ""
        lines.append(f"#{q['task_id']} {q['question']}{opts}{warn(q)}")
    lines.append(f"\nReply like: #{questions[0]['task_id']} your answer")
    return (None, "text", ("\n".join(lines),))
