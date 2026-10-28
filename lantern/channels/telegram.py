"""Telegram fallback channel over the raw Bot API (long polling, no extra dependency).

  getUpdates(offset, timeout, allowed_updates=["message","callback_query"])   receive
  sendMessage(chat_id, text, reply_markup={"inline_keyboard": [[{text, callback_data}]]})
  answerCallbackQuery(callback_query_id, text)                               stop the button spinner
  getFile(file_id) -> file_path; GET https://api.telegram.org/file/bot<token>/<file_path>   voice notes
callback_data is limited to 64 bytes; Lantern's approval buttons use about 50.
Only the owner's chat id (LANTERN_TELEGRAM_OWNER) is listened to.
"""
from __future__ import annotations

import threading
import time
from typing import Callable

from .base import Channel, ChannelError, Http, Incoming, json_call, urllib_http

API = "https://api.telegram.org"


class TelegramChannel(Channel):
    name = "telegram"
    destination = API

    def __init__(self, token: str, owner: str, *, http: Http | None = None, poll_timeout: int = 30):
        if not (token and owner):
            raise ValueError("Telegram needs TELEGRAM_BOT_TOKEN and LANTERN_TELEGRAM_OWNER")
        self.token = token
        self.owner = str(owner)
        self.http = http or urllib_http
        self.poll_timeout = poll_timeout
        self.offset = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _call(self, method: str, payload: dict | None = None) -> dict:
        data = json_call(self.http, "POST", f"{API}/bot{self.token}/{method}", payload or {})
        if not data.get("ok"):
            raise ChannelError(f"telegram {method}: {data.get('description', 'not ok')}")
        return data.get("result")

    # ---- sending ------------------------------------------------------------------
    def send_text(self, text: str) -> None:
        self._call("sendMessage", {"chat_id": self.owner, "text": text[:4096]})

    def send_to(self, address: str, text: str) -> None:
        self._call("sendMessage", {"chat_id": address, "text": text[:4096]})

    def send_buttons(self, text: str, buttons: list[tuple[str, str]]) -> None:
        for bid, _ in buttons:
            if len(bid.encode()) > 64:
                raise ValueError("telegram callback_data is limited to 64 bytes")
        self._call("sendMessage", {"chat_id": self.owner, "text": text[:4096], "reply_markup": {
            "inline_keyboard": [[{"text": title, "callback_data": bid} for bid, title in buttons]]}})

    def acknowledge(self, incoming: Incoming, text: str = "") -> None:
        if incoming.callback_id:
            self._call("answerCallbackQuery", {"callback_query_id": incoming.callback_id, "text": text[:200]})

    # ---- receiving -------------------------------------------------------------------
    def download_file(self, file_id: str) -> tuple[bytes, str]:
        info = self._call("getFile", {"file_id": file_id})
        path = info.get("file_path", "")
        status, data = self.http("GET", f"{API}/file/bot{self.token}/{path}", {}, None)
        if status != 200:
            raise ChannelError(f"telegram file download failed [{status}]")
        ext = "." + path.rsplit(".", 1)[-1] if "." in path else ".ogg"
        return data, ext

    def parse(self, update: dict) -> Incoming | None:
        if "callback_query" in update:
            q = update["callback_query"]
            chat = ((q.get("message") or {}).get("chat") or {}).get("id", (q.get("from") or {}).get("id"))
            return Incoming(self.name, str(chat), "button", button=str(q.get("data", "")),
                            msg_id=str(update.get("update_id")), callback_id=str(q.get("id", "")))
        m = update.get("message")
        if not m:
            return None
        chat = str((m.get("chat") or {}).get("id", ""))
        if m.get("voice") or m.get("audio"):
            fid = (m.get("voice") or m.get("audio"))["file_id"]
            return Incoming(self.name, chat, "voice", msg_id=str(m.get("message_id")),
                            fetch_audio=lambda fid=fid: self.download_file(fid))
        if m.get("video"):   # S12: a how-to video
            fid = m["video"]["file_id"]
            return Incoming(self.name, chat, "video", text=str(m.get("caption", "")), msg_id=str(m.get("message_id")),
                            fetch_video=lambda fid=fid: self.download_file(fid))
        if m.get("photo"):   # S9: the largest size Telegram offers
            fid = max(m["photo"], key=lambda p: p.get("width", 0) * p.get("height", 0))["file_id"]
            return Incoming(self.name, chat, "photo", text=str(m.get("caption", "")), msg_id=str(m.get("message_id")),
                            fetch_image=lambda fid=fid: self.download_file(fid))
        if "text" in m:
            return Incoming(self.name, chat, "text", text=str(m["text"]), msg_id=str(m.get("message_id")))
        return None

    def poll_once(self) -> list[Incoming]:
        updates = self._call("getUpdates", {"offset": self.offset, "timeout": self.poll_timeout,
                                            "allowed_updates": ["message", "callback_query"]})
        out = []
        for u in updates or []:
            self.offset = max(self.offset, int(u.get("update_id", 0)) + 1)
            item = self.parse(u)
            if item is not None:
                out.append(item)
        return out

    def start(self, handle: Callable[[Incoming], None]) -> None:
        def loop() -> None:
            while not self._stop.is_set():
                started = time.monotonic()
                try:
                    items = self.poll_once()
                    for item in items:
                        handle(item)
                except Exception:  # network blips: back off and keep polling
                    self._stop.wait(5)
                    continue
                if not items and time.monotonic() - started < 1:
                    self._stop.wait(1)  # the server answered an empty long poll instantly: don't spin
        self._thread = threading.Thread(target=loop, name="lantern-telegram", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
