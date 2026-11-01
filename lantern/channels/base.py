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
import secrets
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
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


def build_multipart(fields: dict, file_field: str, filename: str, data: bytes,
                    mime: str) -> tuple[bytes, str]:
    """One multipart/form-data body (standard library only): text fields, then one file part."""
    boundary = "lantern-" + secrets.token_hex(16)
    out = bytearray()
    for name, value in fields.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                f"{value}\r\n").encode()
    safe = str(filename).replace('"', "_").replace("\r", "_").replace("\n", "_")
    out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
            f"filename=\"{safe}\"\r\nContent-Type: {mime}\r\n\r\n").encode()
    out += data + f"\r\n--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def multipart_call(http: Http, url: str, fields: dict, file_field: str, filename: str,
                   data: bytes, mime: str, headers: dict | None = None) -> dict:
    """POST one file as multipart/form-data; returns the parsed JSON body."""
    body, content_type = build_multipart(fields, file_field, filename, data, mime)
    h = {"Accept": "application/json", "Content-Type": content_type, **(headers or {})}
    status, raw = http("POST", url, h, body)
    try:
        parsed = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        parsed = {"raw": raw[:200].decode(errors="replace")}
    if status >= 300:
        raise ChannelError(f"POST {url.split('?')[0]} failed [{status}]: {json.dumps(parsed)[:300]}")
    return parsed


IMAGE_MIMES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


def image_mime(path: str | Path) -> str:
    """The MIME type for a sendable image path, or a refusal for anything else."""
    mime = IMAGE_MIMES.get(Path(path).suffix.lower())
    if mime is None:
        raise ChannelError("only PNG or JPEG images can be sent")
    return mime


def read_image(path: str | Path, max_bytes: int) -> tuple[bytes, str]:
    """Read an image file for sending, enforcing its type and a size limit."""
    mime = image_mime(path)
    data = Path(path).read_bytes()
    if len(data) > max_bytes:
        raise ChannelError(f"image is {len(data)} bytes; the limit is {max_bytes}")
    if not data:
        raise ChannelError("image file is empty")
    return data, mime


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

    def send_card(self, text: str, footer: str, buttons: list[tuple[str, str]]) -> None:
        """An approval card: like send_buttons, with a short expiry footer."""
        self.send_buttons(f"{text}\n{footer}" if footer else text, buttons)

    def send_list(self, text: str, button: str, rows: list[tuple[str, str, str]],
                  footer: str = "") -> None:
        """A pick-one menu: rows are (id, title, description). The base fallback
        is numbered plain text; the owner answers by typing the number."""
        numbered = "\n".join(f"{i}. {title}" for i, (_, title, _) in enumerate(rows, 1))
        self.send_text(f"{text}\n{numbered}" + (f"\n{footer}" if footer else ""))

    def send_image(self, path: str | Path, caption: str = "") -> None:
        """Send an image Lantern made, with `caption`. Text-only fallback: the caption as text."""
        self.send_text(f"📷 image: {caption}" if caption else "📷 image")

    def send_to(self, address: str, text: str) -> None:
        """S6: a Lantern Network message to another Lantern's address (not the owner's chat)."""
        raise NotImplementedError(f"{self.name} can't send to other addresses")

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
