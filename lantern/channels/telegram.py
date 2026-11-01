"""Telegram fallback channel over the raw Bot API (long polling, no extra dependency).

  getUpdates(offset, timeout, allowed_updates=["message","callback_query"])   receive
  sendMessage(chat_id, text, reply_markup={"inline_keyboard": [[{text, callback_data}]]})
  answerCallbackQuery(callback_query_id, text)                               stop the button spinner
  getFile(file_id) -> file_path; GET https://api.telegram.org/file/bot<token>/<file_path>   voice notes
callback_data is limited to 64 bytes; Lantern's approval buttons use about 50.
Only the owner's chat id (LANTERN_TELEGRAM_OWNER) is listened to.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from typing import Callable

from .base import Channel, ChannelError, Http, Incoming, json_call, urllib_http

API = "https://api.telegram.org"
MAX_FILE_BYTES = 50 * 1024 * 1024   # bots can't send more than 50 MB
MAX_CAPTION = 1024                  # sendDocument captions are cut at 1024 chars


def _multipart(fields: dict[str, str], filename: str, data: bytes) -> tuple[str, bytes]:
    """Encode text fields plus one file as multipart/form-data, stdlib only."""
    boundary = "----Lantern" + secrets.token_hex(16)
    safe = "".join(c for c in filename if c not in '"\r\n') or "file"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts += [f"--{boundary}".encode(),
                  f'Content-Disposition: form-data; name="{name}"'.encode(), b"",
                  value.encode()]
    parts += [f"--{boundary}".encode(),
              f'Content-Disposition: form-data; name="document"; filename="{safe}"'.encode(),
              b"Content-Type: application/octet-stream", b"", data,
              f"--{boundary}--".encode()]
    return f"multipart/form-data; boundary={boundary}", b"\r\n".join(parts) + b"\r\n"


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

    def send_buttons(self, text: str, buttons: list[tuple[str, str]]) -> None:
        for bid, _ in buttons:
            if len(bid.encode()) > 64:
                raise ValueError("telegram callback_data is limited to 64 bytes")
        self._call("sendMessage", {"chat_id": self.owner, "text": text[:4096], "reply_markup": {
            "inline_keyboard": [[{"text": title, "callback_data": bid} for bid, title in buttons]]}})

    def send_file(self, data: bytes, filename: str, caption: str = "") -> dict:
        """Send a file as a Telegram document (Bot API sendDocument, multipart).
        Returns {"ok": bool, "bytes": int} with the size Telegram reports back."""
        if len(data) > MAX_FILE_BYTES:
            raise ChannelError(f"telegram can't send {filename}: {len(data)} bytes is over the 50 MB bot limit")
        content_type, body = _multipart({"chat_id": self.owner, "caption": caption[:MAX_CAPTION]},
                                       filename, data)
        status, raw = self.http("POST", f"{API}/bot{self.token}/sendDocument",
                                {"Content-Type": content_type}, body)
        try:
            parsed = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            parsed = {}
        if status >= 300 or not parsed.get("ok"):
            raise ChannelError(f"telegram sendDocument failed [{status}]: {json.dumps(parsed)[:200]}")
        try:
            size = int(parsed["result"]["document"]["file_size"])
        except (KeyError, TypeError, ValueError):
            raise ChannelError("telegram sendDocument gave no document file_size") from None
        return {"ok": True, "bytes": size}

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
