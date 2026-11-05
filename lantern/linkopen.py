"""Open a shared link in this laptop's default browser (T1).

The owner shares a YouTube or web link to Lantern's Telegram bot from the
phone (Android: Share -> Telegram -> the bot); Lantern opens it in the
laptop's default browser after one approval. Standard library only.
"""
from __future__ import annotations

import re
import webbrowser
from urllib.parse import urlsplit

_URL_RE = re.compile(r"https?://[^\s<>\"]+")

_TRAILING = ").,!?\"'"

MAX_URL_CHARS = 2048


def clean_url(text: str) -> str | None:
    """Find the first http(s) URL in a shared message, or None.

    Android shares often add a title before the link, e.g.
    "Cool video https://youtu.be/abc?si=x". Trailing punctuation from the
    chat text is stripped. Anything that is not a plain http(s) link without
    userinfo is refused: other schemes, user:password@host, overlong URLs.
    """
    m = _URL_RE.search(text or "")
    if not m:
        return None
    url = m.group(0).rstrip(_TRAILING)
    if len(url) > MAX_URL_CHARS:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    if not parts.hostname:
        return None
    if "@" in parts.netloc:  # user:password@host (any userinfo): refuse
        return None
    return url


def open_url(url: str, opener=webbrowser.open) -> bool:
    """Open url in the default browser; returns what the opener returned."""
    return opener(url)
