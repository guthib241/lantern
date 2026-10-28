"""Channels: ways for the user to reach Lantern from their phone (or glasses, via WhatsApp).

Every channel does the same three things:
  - turns what arrives (text, voice note, button tap) into an `Incoming`,
  - sends text and approval cards (Approve / Deny / Show buttons) to the owner,
  - ignores everyone except the owner (allowlist checked in code).

Lantern on WhatsApp/Telegram is a personal task and approval console for your own
computer, not a general chatbot.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

# http(method, url, headers, body) -> (status, body_bytes); swapped out in tests
Http = Callable[[str, str, dict, "bytes | None"], tuple[int, bytes]]


class ChannelError(RuntimeError):
    """Sending or fetching through a channel failed."""


def urllib_http(method: str, url: str, headers: dict, body: bytes | None = None,
                timeout: float = 40) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ChannelError(f"network error: {e}") from e


def json_call(http: Http, method: str, url: str, payload: dict | None = None,
              headers: dict | None = None) -> dict:
    h = {"Accept": "application/json", **(headers or {})}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        h["Content-Type"] = "application/json"
    status, data = http(method, url, h, body)
    try:
        parsed = json.loads(data or b"{}")
    except json.JSONDecodeError:
        parsed = {"raw": data[:200].decode(errors="replace")}
    if status >= 300:
        raise ChannelError(f"{method} {url.split('?')[0]} failed [{status}]: {json.dumps(parsed)[:300]}")
    return parsed


@dataclass
class Incoming:
    """Something the owner sent. `fetch_audio` downloads a voice note and `fetch_image` a photo (S9):
    () -> (bytes, file_extension). A photo's caption is in `text`."""
    channel: str
    sender: str
    kind: str                      # text | button | voice | photo | video
    text: str = ""
    button: str = ""
    msg_id: str = ""
    callback_id: str = ""          # Telegram: answer the button tap
    fetch_audio: Callable[[], tuple[bytes, str]] | None = field(default=None, repr=False)
    fetch_image: Callable[[], tuple[bytes, str]] | None = field(default=None, repr=False)
    fetch_video: Callable[[], tuple[bytes, str]] | None = field(default=None, repr=False)   # S12: a how-to video


class Channel:
    """Base class. Subclasses implement send_text / send_buttons and parse their own updates."""

    name = "channel"
    owner = ""

    def send_text(self, text: str) -> None:
        raise NotImplementedError

    def send_buttons(self, text: str, buttons: list[tuple[str, str]]) -> None:
        """buttons: [(id, title)], at most 3, titles at most 20 characters."""
        raise NotImplementedError

    def acknowledge(self, incoming: Incoming, text: str = "") -> None:
        """Optional: confirm a button tap in the channel's own UI (Telegram spinner)."""

    def is_owner(self, sender: str) -> bool:
        return bool(self.owner) and normalize_id(sender) == normalize_id(self.owner)

    def start(self, handle: Callable[[Incoming], None]) -> None:
        """Begin receiving (polling channels start a thread; webhooks need nothing)."""

    def stop(self) -> None:
        pass


def normalize_id(value: str) -> str:
    """Phone numbers and chat ids compare by digits ('+91 98765-43210' == '919876543210')."""
    v = str(value).strip()
    digits = "".join(c for c in v if c.isdigit())
    return ("-" if v.startswith("-") else "") + digits if digits else v.lower()
