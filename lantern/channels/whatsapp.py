"""WhatsApp Cloud API channel (Meta's WhatsApp Business Platform).

Receiving (webhook, routes in server.py; exempt from the local token, protected by signature):
  GET  /webhook/whatsapp   handshake: hub.mode=subscribe + hub.verify_token -> echo hub.challenge
  POST /webhook/whatsapp   X-Hub-Signature-256: sha256=<HMAC-SHA256(raw body, app secret)>,
                           checked in constant time on the raw bytes, then 200 right away;
                           processing happens on the hub's worker thread.
  Payload: {"object": "whatsapp_business_account", "entry": [{"changes": [{"value":
            {"messages": [{"from", "id", "type", "text": {"body"} | "interactive":
             {"type": "button_reply", "button_reply": {"id", "title"}} | "audio": {"id", "mime_type", "voice"}}],
             "statuses": [...]}}]}]}
Sending: POST https://graph.facebook.com/<version>/<phone_number_id>/messages with a System User
token. Reply buttons: interactive type "button", at most 3, titles at most 20 characters.
Voice notes: GET /<media-id> -> {url, mime_type, ...}; GET url with the same bearer token
(the URL lives 5 minutes).

Free-form messages (text and buttons) only go out within 24 hours of the owner's last
message, so send Lantern anything ("hi", or a voice note from the glasses) to open the window.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Any

from .base import Channel, ChannelError, Http, Incoming, json_call, urllib_http

GRAPH = "https://graph.facebook.com"
DEFAULT_VERSION = "v23.0"
MAX_BODY = 1024      # interactive message body
MAX_TEXT = 4096
MAX_TITLE = 20


def verify_signature(raw: bytes, header: str | None, app_secret: str) -> bool:
    if not header or not app_secret or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256="):].strip().lower())


def verify_handshake(params: dict, verify_token: str) -> str | None:
    """The challenge to echo back, or None if the handshake is not ours."""
    if (params.get("hub.mode") == "subscribe" and verify_token
            and secrets.compare_digest(str(params.get("hub.verify_token", "")), verify_token)):
        return str(params.get("hub.challenge", ""))
    return None


class WhatsAppChannel(Channel):
    name = "whatsapp"
    destination = GRAPH

    def __init__(self, token: str, phone_number_id: str, owner: str, *, app_secret: str = "",
                 verify_token: str = "", version: str = DEFAULT_VERSION, http: Http | None = None):
        if not (token and phone_number_id and owner):
            raise ValueError("WhatsApp needs WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID and LANTERN_WHATSAPP_OWNER")
        self.token = token
        self.phone_number_id = phone_number_id
        self.owner = owner
        self.app_secret = app_secret
        self.verify_token = verify_token
        self.version = version
        self.http = http or urllib_http

    # ---- sending ------------------------------------------------------------------
    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def _send(self, message: dict, to: str | None = None) -> dict:
        payload = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to or self.owner, **message}
        return json_call(self.http, "POST", f"{GRAPH}/{self.version}/{self.phone_number_id}/messages",
                         payload, self._auth())

    def send_text(self, text: str) -> None:
        self._send({"type": "text", "text": {"preview_url": False, "body": text[:MAX_TEXT]}})

    def send_to(self, address: str, text: str) -> None:
        self._send({"type": "text", "text": {"preview_url": False, "body": text[:MAX_TEXT]}}, to=address)

    def send_buttons(self, text: str, buttons: list[tuple[str, str]]) -> None:
        if not 1 <= len(buttons) <= 3:
            raise ValueError("WhatsApp reply buttons: 1 to 3")
        self._send({"type": "interactive", "interactive": {
            "type": "button",
            "body": {"text": text[:MAX_BODY]},
            "action": {"buttons": [{"type": "reply", "reply": {"id": bid[:256], "title": title[:MAX_TITLE]}}
                                   for bid, title in buttons]},
        }})

    # ---- media ---------------------------------------------------------------------
    def download_media(self, media_id: str) -> tuple[bytes, str]:
        meta = json_call(self.http, "GET", f"{GRAPH}/{self.version}/{media_id}", None, self._auth())
        url = meta.get("url")
        if not url:
            raise ChannelError("media has no download URL")
        status, data = self.http("GET", url, self._auth(), None)
        if status != 200:
            raise ChannelError(f"media download failed [{status}]")
        mime = str(meta.get("mime_type", "audio/ogg")).split(";")[0].strip()
        ext = {"audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/aac": ".aac",
               "audio/amr": ".amr", "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "video/mp4": ".mp4",
               "video/3gpp": ".3gp"}.get(mime, ".ogg")
        return data, ext

    # ---- receiving -------------------------------------------------------------------
    def parse(self, payload: dict) -> list[Incoming]:
        """Turn a webhook notification into Incoming items. Status updates are ignored."""
        out: list[Incoming] = []
        if payload.get("object") != "whatsapp_business_account":
            return out
        for entry in payload.get("entry") or []:
            for change in entry.get("changes") or []:
                value: dict[str, Any] = change.get("value") or {}
                for m in value.get("messages") or []:
                    item = self._message(m)
                    if item is not None:
                        out.append(item)
        return out

    def _message(self, m: dict) -> Incoming | None:
        sender, kind, mid = str(m.get("from", "")), m.get("type"), str(m.get("id", ""))
        if kind == "text":
            return Incoming(self.name, sender, "text", text=str((m.get("text") or {}).get("body", "")), msg_id=mid)
        if kind == "interactive":
            reply = (m.get("interactive") or {}).get("button_reply") or {}
            if reply.get("id"):
                return Incoming(self.name, sender, "button", button=str(reply["id"]), msg_id=mid)
            return None
        if kind == "button":  # a tap on a template's quick-reply button
            b = m.get("button") or {}
            return Incoming(self.name, sender, "button", button=str(b.get("payload") or b.get("text", "")), msg_id=mid)
        if kind == "audio":
            media_id = str((m.get("audio") or {}).get("id", ""))
            if not media_id:
                return None
            return Incoming(self.name, sender, "voice", msg_id=mid,
                            fetch_audio=lambda media_id=media_id: self.download_media(media_id))
        if kind == "video":   # S12: a how-to video the user shares ("do this")
            vid = m.get("video") or {}
            if not vid.get("id"):
                return None
            return Incoming(self.name, sender, "video", text=str(vid.get("caption", "")), msg_id=mid,
                            fetch_video=lambda media_id=str(vid["id"]): self.download_media(media_id))
        if kind == "image":   # S9: a photo, e.g. from the glasses ("send a photo to Lantern")
            img = m.get("image") or {}
            if not img.get("id"):
                return None
            return Incoming(self.name, sender, "photo", text=str(img.get("caption", "")), msg_id=mid,
                            fetch_image=lambda media_id=str(img["id"]): self.download_media(media_id))
        return None
