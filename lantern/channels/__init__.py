"""Channels: WhatsApp (primary), Telegram (fallback) and a simulated phone, behind one hub.

LANTERN_CHANNELS picks which run together (comma list, default "fake"):
  fake       simulated phone (tests, rehearsal, the desktop's demo mode)
  whatsapp   WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_APP_SECRET, WHATSAPP_VERIFY_TOKEN,
             LANTERN_WHATSAPP_OWNER (your number, digits), WHATSAPP_GRAPH_VERSION (optional)
  telegram   TELEGRAM_BOT_TOKEN, LANTERN_TELEGRAM_OWNER (your chat id)
The local HTTP API (desktop app, scripts) is always on, next to these.
Secrets come only from the environment / .env, never from config.json.
"""
from __future__ import annotations

from .base import Channel, ChannelError, Incoming
from .fake import FakeChannel
from .hub import ChannelHub
from .telegram import TelegramChannel
from .whatsapp import DEFAULT_VERSION, WhatsAppChannel, verify_handshake, verify_signature

__all__ = ["Channel", "ChannelError", "ChannelHub", "FakeChannel", "Incoming", "TelegramChannel",
           "WhatsAppChannel", "channels_from_env", "verify_handshake", "verify_signature"]


def channels_from_env(env: dict) -> list[Channel]:
    names = env.get("LANTERN_CHANNELS") or env.get("LANTERN_CHANNEL") or "fake"
    out: list[Channel] = []
    for name in [n.strip().lower() for n in names.split(",") if n.strip()]:
        if name == "fake":
            out.append(FakeChannel())
        elif name == "whatsapp":
            out.append(WhatsAppChannel(
                env.get("WHATSAPP_TOKEN", ""), env.get("WHATSAPP_PHONE_NUMBER_ID", ""),
                env.get("LANTERN_WHATSAPP_OWNER", ""), app_secret=env.get("WHATSAPP_APP_SECRET", ""),
                verify_token=env.get("WHATSAPP_VERIFY_TOKEN", ""),
                version=env.get("WHATSAPP_GRAPH_VERSION", DEFAULT_VERSION)))
        elif name == "telegram":
            out.append(TelegramChannel(env.get("TELEGRAM_BOT_TOKEN", ""), env.get("LANTERN_TELEGRAM_OWNER", "")))
        elif name not in ("none", "off"):
            raise ValueError(f"unknown channel {name!r}; use fake, whatsapp, telegram or none")
    return out
