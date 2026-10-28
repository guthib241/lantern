"""A simulated phone: records what Lantern sends and lets tests (or the desktop's demo mode)
inject messages as if the owner typed them, tapped a button or sent a voice note."""
from __future__ import annotations

import threading
import time
from collections import deque

from .base import Channel, Incoming

FAKE_OWNER = "owner"


class FakeChannel(Channel):
    name = "fake"

    def __init__(self, owner: str = FAKE_OWNER, keep: int = 200):
        self.owner = owner
        self.sent: deque = deque(maxlen=keep)
        self._lock = threading.Lock()
        self.acks: list[tuple[str, str]] = []

    def send_text(self, text: str) -> None:
        with self._lock:
            self.sent.append({"type": "text", "text": text, "ts": time.time()})

    def send_buttons(self, text: str, buttons: list[tuple[str, str]]) -> None:
        with self._lock:
            self.sent.append({"type": "buttons", "text": text, "ts": time.time(),
                              "buttons": [{"id": b, "title": t} for b, t in buttons]})

    def acknowledge(self, incoming: Incoming, text: str = "") -> None:
        self.acks.append((incoming.button, text))

    def note_owner(self, text: str = "", button: str = "") -> None:
        """Show the owner's side of the chat too (typed text, or the title of the tapped button)."""
        if button:
            titles = [b["title"] for m in self.sent for b in m.get("buttons", []) if b["id"] == button]
            text = titles[-1] if titles else "(tapped a button)"
        with self._lock:
            self.sent.append({"type": "text", "text": text, "ts": time.time(), "from_owner": True})

    def messages(self) -> list[dict]:
        with self._lock:
            return list(self.sent)

    @staticmethod
    def incoming(text: str = "", button: str = "", sender: str = FAKE_OWNER, audio: bytes | None = None,
                 ext: str = ".ogg", image: bytes | None = None, video: bytes | None = None) -> Incoming:
        if audio is not None:
            return Incoming("fake", sender, "voice", fetch_audio=lambda: (audio, ext))
        if video is not None:   # a video; `text` is its caption
            return Incoming("fake", sender, "video", text=text, fetch_video=lambda: (video, ".mp4"))
        if image is not None:   # a photo; `text` is its caption
            return Incoming("fake", sender, "photo", text=text, fetch_image=lambda: (image, ".jpg"))
        if button:
            return Incoming("fake", sender, "button", button=button)
        return Incoming("fake", sender, "text", text=text)
