"""The hub between channels (WhatsApp, Telegram, the simulated phone) and Lantern's core.

Incoming, from the owner only (everyone else is ignored and counted):
  text          -> a new task (tagged with the channel), or
  "STOP"        -> the kill switch: cancel the running task, deny every pending approval
  "stop #N"     -> stop only task N (its open question and approval cards too); the others carry on
  "status"      -> what Lantern is doing
  "menu"/"hi"   -> a pick-one menu (status, reminders, undo, summary, stop) as a list message
  "summary"     -> today's short summary, written by Muse Spark from Lantern's own records
  "resume"      -> run the tasks cut short when Lantern stopped
  "undo"        -> put back the files the last task moved (one approval, no model call)
  "loops"       -> scheduled agents and whether each loop is on; "pause <name>" turns one off
  "agents"      -> installed agents that run on the phone, with Run buttons
  "run <name> key=value ..." -> start a phone agent (a laptop-only agent is refused)
  "receipt [N]" -> the last finished task's receipt (or task N's): changes, checks, approvals, data sent, cost
  "#N answer"   -> the answer to task N's question (ask_user; the task was parked, scheduler.py), also by button
  "learn [N]"   -> compile the last finished task (or task N) into a routine that runs with no model calls (S4)
  voice note    -> download -> ffmpeg 16 kHz WAV -> Muse Voice Transcribe (biased toward the
                   names Lantern knows) -> handled like text, with the transcript echoed back
  button tap    -> approve / deny / show an approval (cards.py); a menu row; "stop all?" yes/no;
                   a suggested next step (starts a new task). Approval button ids carry the nonce
                   and the first 8 hex of its payload hash, so only the card that was shown can
                   answer, and only for the action it showed. "run:<name>" starts a phone agent.
Outgoing (core events): every approval request becomes a card with Approve / Deny / Show
buttons on every channel (the expiry line is the footer on WhatsApp, text elsewhere),
unless its task is a laptop-only agent (the panel shows those); a finished task's reply
goes back to the channel it came from, with up to 2 suggested next steps as buttons
(suggest.py; just new requests, through the normal agent), and a scheduled agent run
with no channel reports to its pack's surface.
Questions from parked tasks that arrive within a second of each other go out as one message.

Work happens on two small threads (inbox, outbox), so webhooks answer fast and a slow
network never blocks the agent. Outgoing text is scrubbed and audited like any cloud call.
"""
from __future__ import annotations

import datetime as dt
import json
import queue
import re
import secrets
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
from ..network import PREFIX as NET_PREFIX
from ..voice import to_wav16k, transcribe
from . import daily as daily_mod
from . import suggest as suggest_mod
from .base import Channel, Incoming
from .cards import approval_card

STOP = re.compile(r"^\W*/?stop\b", re.I)
STOP_ONE = re.compile(r"^\W*/?stop\s+(?:task\s+)?#?(\d+)\W*$", re.I)   # "stop #12": that task only
STATUS = re.compile(r"^\W*/?status\W*$", re.I)
MENU = re.compile(r"^\W*/?(menu|hi|help|start)\W*$", re.I)
SUMMARY = re.compile(r"^\W*/?summary\W*$", re.I)
NUMBER = re.compile(r"^[1-9]$")
RESUME = re.compile(r"^\W*/?(resume|continue)\W*$", re.I)
RECEIPT = re.compile(r"^\W*/?receipt(?:\s+(?:for\s+)?(?:task\s+)?#?(\d+))?\W*$", re.I)
ANSWER = re.compile(r"^\s*#(\d+)[\s:,.-]+(.+)$", re.S)   # "#12 residential": an answer to task 12's question
LEARN = re.compile(r"^\W*/?learn(?:\s+(?:it|this|that|task))?(?:\s+#?(\d+))?\W*$", re.I)
# S16: a photo of handwritten working with these words goes to Check my work, not to the scanner
CHECK_WORK = re.compile(r"\bcheck\s+(?:my|this|the)?\s*(?:work|working|homework|answer|solution|steps|math|maths)\b", re.I)
QUESTION_DELAY = 1.0   # seconds: questions that arrive together go out as one message
LOOPS = re.compile(r"^\W*/?loops\W*$", re.I)
PAUSE = re.compile(r"^\W*/?pause\s+([A-Za-z0-9][A-Za-z0-9-]*)\W*$", re.I)
AGENTS = re.compile(r"^\W*/?agents\W*$", re.I)
RUN = re.compile(r"^\W*/?run\s+([A-Za-z0-9][A-Za-z0-9-]*)", re.I)
LINK_OPEN = re.compile(r"^\W*(?:open\s+this\s+on\s+my\s+laptop\s+|open\s+)?(https?://\S+)\W*$", re.I)
LAPTOP_ONLY = "this agent runs on the laptop only; change it in the Agents tab"
SCOPE = ("I'm Lantern, your computer's task console. Send me a task (\"fill the reimbursement form\", "
         "\"which projects use torch?\", \"remind me at 6 to call Priya\"), STOP to halt everything, status, "
         "resume, undo (puts back the files the last task moved), receipt (proof of what the last task did), or "
         "learn (turn the last task into a routine that runs with no model calls). Send a photo of a paper (from "
         "your phone or glasses) and say what it's for: I scan it and run the routine with the scan attached. Send a "
         "how-to video and I'll send back its steps to check before I do them. Send a photo of your math working with "
         "\"check my work\" and I'll find the first line that doesn't follow. "
         "Agents that run on the phone: send agents to list them, run <name> key=value to start one. "
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
        self.borrowed: dict[int, tuple[str, str, str]] = {}   # S13: task id -> (channel, borrower address, name)
        self.pack_of: dict[int, str] = {}         # task id -> agent pack it runs (for surface routing)
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
        self._suggestions: dict[str, str] = {}   # short id -> full suggested request (server-side, never in buttons)
        self._suggest_lock = threading.Lock()
        self._menus: dict[str, list[str]] = {}   # channel -> last menu's row ids (for typed numbers)
        self._daily_sent: set[str] = daily_mod.load_sent(core)   # persisted: a restart must not resend
        self._check_images: dict[int, str] = {}   # check-work task id -> circled image waiting for its reply
        self._daily_stop = threading.Event()
        self.photo_wait = PHOTO_WAIT              # S9: seconds to wait for words after a photo without a caption
        self._photos: dict[str, tuple[bytes, int]] = {}   # channel -> (photo waiting for words, its number)
        self._photo_n = 0
        self._photo_lock = threading.Lock()

    # ---- lifecycle -------------------------------------------------------------------
    def _net_send(self, contact: dict, text: str) -> None:
        """S6: a signed message to another Lantern, on the channel and address its card was added with."""
        ch = self.channels.get(contact.get("channel", ""))
        if ch is None:
            raise ValueError(f"no {contact.get('channel')} channel here to reach {contact.get('name')}")
        ch.send_to(contact["address"], text)

    def start(self, listen: bool = True) -> "ChannelHub":
        """Start the workers; `listen` also starts polling channels (tests drive them by hand)."""
        self._unsubscribe = self.core.subscribe(self._on_event)
        if hasattr(self.core, "network_send"):
            self.core.network_send = self._net_send
        for name, fn in (("lantern-inbox", self._inbox_loop), ("lantern-outbox", self._outbox_loop)):
            t = threading.Thread(target=fn, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        if listen:
            for c in self.channels.values():
                c.start(self.receive)
            self._daily_stop.clear()   # the daily summary (daily.py) is only polled on the serving path
            t = threading.Thread(target=self._daily_loop, name="lantern-daily", daemon=True)
            t.start()
            self._threads.append(t)
        if hasattr(self.core, "questions") and self.core.questions():   # asked before a restart: ask again
            self._schedule_questions()
        cut = getattr(self.core, "interrupted", [])
        if cut:
            names = "; ".join(f"#{t.id} {t.goal.split(chr(10))[0][:60]}" for t in cut[:3])
            self.say(None, f"🏮 I was stopped in the middle of {len(cut)} task{'s' if len(cut) != 1 else ''}: "
                           f"{names}{' …' if len(cut) > 3 else ''}. Reply resume to continue.")
        return self

    def stop(self) -> None:
        self._daily_stop.set()
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

    def _media_image(self, path: str | Path) -> Path:
        """Resolve an image Lantern made; refuse anything outside its media folder (never a user file)."""
        resolved = Path(path).expanduser().resolve()
        media = self.media_dir.expanduser().resolve()
        if not resolved.is_relative_to(media) or not resolved.is_file():
            raise ValueError("only images Lantern made in its media folder can be sent")
        if resolved.suffix.lower() not in (".png", ".jpg", ".jpeg"):
            raise ValueError("only PNG or JPEG images can be sent")
        return resolved

    def _is_private(self, task_id: int | None, event: dict | None = None) -> bool:
        """True when a task's reply must stay on the laptop, so its image must not leave either."""
        if event is not None and "reply_public" in event and event["reply_public"] != event.get("reply"):
            return True
        if task_id is not None:
            try:
                return bool(self.core.memory.is_private_task(task_id))
            except Exception:
                return False
        return False

    def say_image(self, channel: str | None, path: str | Path, caption: str, *,
                  task_id: int | None = None, event: dict | None = None) -> bool:
        """Queue an image Lantern made for one channel (or every channel). False when refused:
        outside the media folder, or a private task's reply. The caption is scrubbed on delivery."""
        if self._is_private(task_id, event):
            self._error("image", ValueError("a private task's image never leaves the laptop"))
            return False
        try:
            resolved = self._media_image(path)
        except (ValueError, OSError) as e:
            self._error("image", e)
            return False
        self._outbox.put((channel, "image", (str(resolved), caption, task_id)))
        return True

    def _deliver(self, ch: Channel, kind: str, args: tuple) -> None:
        extra: dict = {}
        if kind == "image":
            return self._deliver_image(ch, args)
        if kind == "card":
            text, footer, buttons = args[0], args[1], args[2]
            text, footer = SCRUBBER.scrub(text).text, SCRUBBER.scrub(footer).text
            ch.send_card(text, footer, buttons)
            extra = {"footer": footer, "buttons": buttons}
        elif kind == "list":
            text, button, rows = args[0], args[1], args[2]
            footer = args[3] if len(args) > 3 else ""
            text, footer = SCRUBBER.scrub(text).text, SCRUBBER.scrub(footer).text if footer else ""
            ch.send_list(text, button, rows, footer)
            extra = {"button": button, "rows": rows, "footer": footer}
        else:
            text = SCRUBBER.scrub(args[0]).text
            if kind == "text":
                ch.send_text(text)
            elif kind == "to":   # S13: to someone this Lantern is lent to, not the owner's chat
                ch.send_to(args[1], text)
                extra = {"to": args[1]}
            else:
                ch.send_buttons(text, args[1])
                extra = {"buttons": args[1]}
        destination = getattr(ch, "destination", None)
        if destination:  # what left the laptop through this channel
            body = json.dumps({"text": text, **{k: v for k, v in extra.items() if k != "to"},
                               "to": extra.get("to", "")}, default=str).encode()
            self.core.audit.record(lane="cloud", destination=destination, endpoint=f"{ch.name}.send",
                                   model=ch.name, payload=body, preview=text)

    def _deliver_image(self, ch: Channel, args: tuple) -> None:
        """Send one circled image: media-folder check again, scrubbed caption, audited by byte hash."""
        path, caption = args[0], args[1]
        task_id = args[2] if len(args) > 2 else None
        if self._is_private(task_id):
            raise ValueError("a private task's image never leaves the laptop")
        resolved = self._media_image(path)   # checked again on the sending thread
        data = resolved.read_bytes()
        text = SCRUBBER.scrub(caption or "").text
        ch.send_image(resolved, text)
        destination = getattr(ch, "destination", None)
        if destination:  # what left the laptop: hash of the bytes, destination and size (audit.record)
            real = type(ch).send_image is not Channel.send_image
            payload = data if real else json.dumps({"text": text}).encode()
            preview = text if real else f"📷 image: {text}"
            self.core.audit.record(lane="cloud", destination=destination,
                                   endpoint=f"{ch.name}.send_image" if real else f"{ch.name}.send",
                                   model=ch.name, payload=payload, preview=preview, task_id=task_id)

    def say(self, channel: str | None, text: str) -> None:
        self._outbox.put((channel, "text", (text,)))

    def say_list(self, channel: str, text: str, button: str,
                 rows: list[tuple[str, str, str]], footer: str = "") -> None:
        self._menus[channel] = [rid for rid, _, _ in rows]
        self._outbox.put((channel, "list", (text, button, rows, footer)))

    def tell(self, channel: str, address: str, text: str) -> None:
        """S13: a message to a borrower's own chat."""
        self._outbox.put((channel, "to", (text, address)))

    # ---- incoming ----------------------------------------------------------------------
    def handle(self, inc: Incoming) -> None:
        ch = self.channels.get(inc.channel)
        if ch is not None and inc.kind == "text" and inc.text.startswith(NET_PREFIX):
            # S6: another Lantern. Its signature is checked against the contact list (network.py), not the sender.
            try:
                self.core.network_receive(inc.text)
            except Exception as e:
                self._error("network", e)
            return
        if ch is not None and not ch.is_owner(inc.sender) and (grant := self._grant(ch, inc.sender)) is not None:
            return self._borrower(ch, inc, grant)   # S13: someone this Lantern is lent to, right now
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

    # ---- lend your Lantern (S13): a borrower's own chat ---------------------------------------------
    def _grant(self, ch: Channel, sender: str) -> dict | None:
        try:
            return self.core.lending().find(ch.name, sender)
        except Exception:   # no lending here (or its file can't be read): a stranger, as before
            return None

    def _borrower(self, ch: Channel, inc: Incoming, grant: dict) -> None:
        if inc.msg_id:
            key = (inc.channel, inc.msg_id)
            if key in self._seen:
                return
            self._seen.append(key)
        answer = lambda text: self.tell(ch.name, grant["address"], text)   # noqa: E731  (the address the owner gave)
        if inc.kind == "voice":
            text = self._voice(ch, inc, reply=answer)
            if text is None:
                return
        elif inc.kind == "text":
            text = inc.text
        else:
            return answer("Send what you'd like Lantern to do as a message or a voice note.")
        if not (text or "").strip():
            return
        try:
            task_id = self.core.borrow(grant["id"], text)
        except ValueError as e:
            return answer(f"Lantern can't do that right now: {e}.")
        self.borrowed[task_id] = (ch.name, grant["address"], grant["name"])
        answer(f"🔑 On it (task #{task_id}). The owner approves each step on their phone.")
        self.say(None, f"🔑 {grant['name']} asked your Lantern: “{text[:300]}” (task #{task_id}). Each step needs your OK.")

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
        if CHECK_WORK.search(words or ""):
            try:
                task_id = self.core.check_work(image)
            except ValueError as e:
                return self.say(ch.name, f"I can't check work right now: {e}.")
            self.origin[task_id] = ch.name
            return self.say(ch.name, f"✏️ Checking it line by line (task #{task_id}).")
        try:
            task_id = self.core.photo(image, words)
        except ValueError as e:
            return self.say(ch.name, f"I can't scan photos right now: {e}.")
        self.origin[task_id] = ch.name
        self.say(ch.name, f"📷 Scanning it (task #{task_id}).")

    MENU_ROWS = [
        ("menu:status", "Status", "What Lantern is doing"),
        ("menu:reminders", "Reminders", "What's coming up"),
        ("menu:undo", "Undo last task", "Put moved files back"),
        ("menu:summary", "Today's summary", "Today in a few lines"),
        ("menu:stop", "Stop everything", "Halt all tasks"),
    ]
    MENU_TEXT = "🏮 Lantern menu — tap one (or reply with its number):"

    def _do_stop(self, ch: Channel) -> None:
        r = self.core.stop()
        n = r["denied_approvals"]
        self.say(ch.name, "🛑 Stopped. " + ("I cancelled the running task" if r.get("task_id") else "Nothing was running")
                 + (f" and denied {n} pending approval{'s' if n != 1 else ''}." if n else "."))

    def _do_undo(self, ch: Channel) -> None:
        try:
            task_id = self.core.undo_last()
        except UndoError as e:
            return self.say(ch.name, f"Nothing to undo: {e}.")
        self.origin[task_id] = ch.name
        self.say(ch.name, f"↩️ Undo is task #{task_id}. Approve the card to put the files back.")

    def _send_menu(self, ch: Channel) -> None:
        self.say_list(ch.name, self.MENU_TEXT, "Menu", list(self.MENU_ROWS))

    def _reminders_text(self) -> str:
        upcoming = self.core.memory.reminders()[:5]
        if not upcoming:
            return "No reminders pending."
        return "⏰ Reminders:\n" + "\n".join(f"• {r.public()['due']}: {r.text[:80]}" for r in upcoming)

    def _send_daily(self, channel: str | None) -> None:
        self.say(channel, daily_mod.render(self.core))

    def _text(self, ch: Channel, text: str) -> None:
        t = (text or "").strip()
        if not t:
            return
        if (one := STOP_ONE.match(t)) and hasattr(self.core, "stop_task"):
            n = int(one.group(1))
            r = self.core.stop_task(n)
            return self.say(ch.name, f"🛑 Stopping #{n}; the other tasks carry on." if r.get("stopped")
                            else f"#{n} isn't running. (STOP on its own stops everything.)")
        if STOP.match(t):
            return self._do_stop(ch)
        if STATUS.match(t):
            return self.say(ch.name, self._status())
        if MENU.match(t):
            return self._send_menu(ch)
        if SUMMARY.match(t):
            return self._send_daily(ch.name)
        if NUMBER.match(t) and (ids := self._menus.get(ch.name)) and int(t) <= len(ids):
            return self._menu_row(ch, ids[int(t) - 1])
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
            return self._do_undo(ch)
        if m := LINK_OPEN.match(t):  # phone fast path: no model call, faster and free
            url = clean_url(m.group(1))
            if url is not None:
                task_id = self.core.submit(f"[{ch.name}] open this link on my laptop: {url}")
                self.origin[task_id] = ch.name
                return self.say(ch.name, f"🏮 On it (task #{task_id}).")
        busy = getattr(self.core, "current_task", None)
        waiting = busy is not None and any(True for _ in self.core.bus.pending())
        task_id = self.core.submit(f"[{ch.name}] {t}")
        self.origin[task_id] = ch.name
        if busy is None:
            self.say(ch.name, f"🏮 On it (task #{task_id}).")
        else:   # tasks run one at a time: say so instead of "on it" (live run, Oct 27: it sat queued for 83 s)
            why = " It's waiting for your approval." if waiting else ""
            self.say(ch.name, f"🏮 Queued as task #{task_id}: I'll start right after task #{busy}.{why}")

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

    def _menu_row(self, ch: Channel, row_id: str) -> None:
        """A menu row tap (or its typed number): status, reminders, undo, summary, or stop."""
        parts = row_id.split(":")
        sub, extra = (parts[1] if len(parts) > 1 else ""), (parts[2] if len(parts) > 2 else "")
        if sub == "status":
            return self.say(ch.name, self._status())
        if sub == "reminders":
            return self.say(ch.name, self._reminders_text())
        if sub == "undo":
            return self._do_undo(ch)
        if sub == "summary":
            return self._send_daily(ch.name)
        if sub == "stop":
            if extra == "yes":
                return self._do_stop(ch)
            if extra == "no":
                return self.say(ch.name, "Okay, still running.")
            self._outbox.put((ch.name, "buttons", ("Stop all tasks?", [("menu:stop:yes", "Yes"),
                                                                       ("menu:stop:no", "No")])))
            return
        self.say(ch.name, "That menu choice isn't open any more — send menu to see it again.")

    def _suggest_tap(self, ch: Channel, inc: Incoming, sid: str) -> None:
        """A tap on a suggested next step: a new task with the stored request."""
        with self._suggest_lock:
            request = self._suggestions.pop(sid, None)
        if not request:
            return self.say(ch.name, "That suggestion has expired — just type what you'd like.")
        task_id = self.core.submit(f"[{ch.name}] {request}")
        self.origin[task_id] = ch.name
        self.say(ch.name, f"🏮 On it (task #{task_id}).")

    def _button(self, ch: Channel, inc: Incoming) -> None:
        parts = inc.button.split(":")
        verb, aid = parts[0], parts[1] if len(parts) > 1 else ""
        if verb == "menu":
            return self._menu_row(ch, inc.button)
        if verb == "suggest":
            return self._suggest_tap(ch, inc, aid)
        if verb == "answer":
            return self._answer_button(ch, inc, parts)
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

    def _voice(self, ch: Channel, inc: Incoming, reply: Callable[[str], None] | None = None) -> str | None:
        reply = reply or (lambda text: self.say(ch.name, text))
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
            # Live API, Oct 27: Hindi and Hinglish notes came back perfectly with languageBias ["Hindi"],
            # and garbled or empty on autodetect, so the user's language goes along when it's configured.
            langs = list(getattr(getattr(self.core, "cfg", None), "voice_languages", None) or [])
            result = (self.transcriber(wav, keywords=self.keywords(), language_bias=langs) if langs
                      else self.transcriber(wav, keywords=self.keywords()))
        except Exception as e:  # download, ffmpeg (CalledProcessError), transcription: all end the same way
            self._error(f"{ch.name} voice", e)
            reply(f"I couldn't hear that voice note ({type(e).__name__}). Try again or type it.")
            return None
        text = str(result.get("transcript", "")).strip()
        ms = int(result.get("audioDurationMs") or 0)
        cost = models.audio_cost(ms)
        self.core.memory.log_usage(None, models.VOICE, cost_usd=cost)
        self.core.emit({"type": "usage", "model": models.VOICE, "badge": models.badge(models.VOICE),
                        "audio_ms": ms, "cost_usd": cost})
        if not text:
            reply("I got the voice note but couldn't make out any words.")
            return None
        reply(f"🎙️ “{text}”")
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
        if kind == "queued" and event.get("task_id") is not None and event.get("pack"):
            # a pack run: remember it, so cards and results follow its surface. Other queued tasks go on down the
            # chain (an automatic learn reports back to the chat that asked for it).
            if len(self.pack_of) > 1000:
                self.pack_of.pop(next(iter(self.pack_of)))
            self.pack_of[event["task_id"]] = event["pack"]
            return
        if kind == "approval" and event.get("event") == "requested":
            if self._surface_muted(event.get("task_id")):
                return
            card = approval_card(event["approval"], time.time())
            self._outbox.put((None, "card", (card.text, card.footer, card.buttons)))
        elif kind == "checkwork" and event.get("image"):
            try:
                self._check_images[int(event.get("task_id"))] = str(event["image"])
            except (TypeError, ValueError):
                pass
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
        elif kind == "grant":   # S13: a loan starts or ends; the borrower and the owner hear about it
            ch, who = event.get("channel", ""), event.get("address", "")
            if ch not in self.channels:
                return
            if event.get("event") == "lent":
                until = time.strftime("%H:%M", time.localtime(float(event.get("expires_at") or 0)))
                self.tell(ch, who, f"🏮 This Lantern is lent to you until {until}: {event.get('text', '')}. Write or "
                                   "send a voice note here; the owner approves each step.")
            else:
                ended = "was revoked" if event.get("event") == "revoked" else "has ended"
                self.tell(ch, who, f"🔑 The loan of this Lantern {ended}. Thank you!")
                self.say(None, f"🔑 {event.get('name')}'s loan {ended}.")
        elif kind == "done" and event.get("id") in self.borrowed:   # S13: reply to the borrower, receipt to the owner
            self._check_images.pop(event.get("id"), None)   # a borrower's chat never gets the image either
            ch, who, name = self.borrowed.pop(event["id"])
            reply = (event.get("reply_public") or event.get("reply") or event.get("summary") or "").strip()
            self.tell(ch, who, reply or "That didn't work.")
            self.say(None, f"🔑 {name}'s task #{event['id']} finished ({event.get('status')}).\n"
                           + self._receipt(event["id"]))
        elif kind == "loop" and event.get("event") == "paused":
            if event.get("name") and self._pack_surface(event["name"]) == "laptop":
                return   # a laptop-only loop pauses quietly; the panel shows it
            self._outbox.put((None, "text", (f"⏸️ {event.get('text', 'A loop paused itself.')}",)))
        elif kind == "done":
            task_id = event.get("id")
            name = self.origin.pop(task_id, None)
            pack = self.pack_of.pop(task_id, None)
            if name:
                self._finished[task_id] = name
                while len(self._finished) > 50:
                    self._finished.pop(next(iter(self._finished)))
            image = self._check_images.pop(task_id, None)
            if pack is not None and self._pack_surface(pack) == "laptop":
                return   # a laptop-only run reports to the panel, never the phone
            if event.get("status") == "cancelled":   # STOP already got its own reply
                return
            # S0: a reply that quotes private content goes out with that part kept on the computer
            reply = (event.get("reply_public") or event.get("reply") or event.get("summary") or "").strip()
            status = event.get("status")
            if status == "failed" and not reply:
                reply = f"That didn't work: {event.get('summary', '')}"
            if not reply:
                return
            if name:
                sent = bool(image and status == "done"
                            and self.say_image(name, image, reply, task_id=task_id, event=event))
                if not sent:
                    self.say(name, reply)
                self._maybe_suggest(event, name, reply)
            elif pack is not None:
                # A scheduled run has no channel: it reports to its pack's surface.
                self._outbox.put((None, "text", (reply,)))


    def _maybe_suggest(self, event: dict, channel: str, reply: str) -> None:
        """Up to 2 suggested next steps as buttons under a finished task's reply, made off the event thread (the
        Spark call must not hold up the next task)."""
        if event.get("status") != "done" or not reply:
            return
        if "reply_public" in event and event["reply_public"] != event.get("reply"):
            return   # private reply: never suggest from it
        if event.get("tainted", True):
            return   # the task read untrusted content (or didn't say): a suggestion could carry planted words
        threading.Thread(target=self._suggest, args=(event, channel, reply), name="lantern-suggest",
                         daemon=True).start()

    def _suggest(self, event: dict, channel: str, reply: str) -> None:
        try:
            task_id = int(event.get("id"))
            goal = self.core.memory.get_task(task_id).goal
        except (TypeError, ValueError, KeyError):
            return
        goal = re.sub(r"^\[[^\]]+\]\s*", "", goal.strip())
        try:
            items = suggest_mod.generate(self.core, task_id, goal, reply)
        except Exception:
            return
        if not items:
            return
        buttons = []
        with self._suggest_lock:
            for title, request in items:
                sid = secrets.token_hex(4)
                self._suggestions[sid] = request
                buttons.append((f"suggest:{sid}", title))
            while len(self._suggestions) > 20:   # tapped or stale suggestions never pile up
                self._suggestions.pop(next(iter(self._suggestions)))
        # the full requests are on screen: a tap sends exactly what the owner read, nothing hidden behind a title
        lines = "\n".join(f"• {title}: {request}" for title, request in items)
        self._outbox.put((channel, "buttons", (f"💡 You could also ask:\n{lines}", buttons)))

    # ---- daily summary (daily.py): once a day on the serving path ---------------------------
    def _daily_loop(self) -> None:
        while not self._daily_stop.wait(30):
            try:
                self.check_daily()
            except Exception as e:  # a bad row must never stop the timer
                self._error("daily", e)

    def check_daily(self, now: float | None = None) -> bool:
        """Send today's summary to every channel if it's due. True if sent.
        The sent date is persisted first, so a restart after the time must not send it again."""
        now = time.time() if now is None else now
        self._daily_sent |= daily_mod.load_sent(self.core)   # another hub may have sent it
        if not daily_mod.due(self._daily_sent, now):
            return False
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        self._daily_sent.add(day)
        daily_mod.record_sent(self.core, day)
        self.say(None, daily_mod.render(self.core, now))
        return True


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
