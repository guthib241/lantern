"""Widgets: tiny self-contained HTML tools Lantern writes for you (a timer, a counter, a checklist).

Three layers keep them harmless (see docs/WIDGETS.md):
  1. A static checker (here) rejects anything that could reach the network or the host:
     fetch, XMLHttpRequest, WebSocket, EventSource, workers, sendBeacon, eval/Function, imports,
     external scripts/styles/frames, any http(s) URL, cookies, top/parent/opener, raw
     postMessage, storage APIs, and common obfuscation tricks. One retry with the errors, then fail.
  2. The desktop runs a widget in <iframe sandbox="allow-scripts"> (no same-origin) inside
     widget-host.html, whose CSP is default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'.
  3. The only bridge is lantern.notify(text) and lantern.storage.get/set(key, value), re-checked
     by the panel.
Saving a widget needs approval; it is written to ~/.lantern/widgets/<name>.html (Lantern's own
folder) and checked again every time it is served. Widgets give no health or medical advice.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import models

REPO_WIDGETS = Path(__file__).resolve().parent.parent / "widgets"
MAX_BYTES = 100_000
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")

BANNED: list[tuple[str, re.Pattern]] = [(why, re.compile(rx, flags)) for why, rx, flags in [
    ("network: fetch()", r"\bfetch\s*\(", 0),
    ("network: XMLHttpRequest", r"XMLHttpRequest", 0),
    ("network: WebSocket", r"WebSocket", 0),
    ("network: EventSource", r"EventSource", 0),
    ("network: sendBeacon", r"sendBeacon", 0),
    ("background workers", r"\b(Shared)?Worker\s*\(|serviceWorker|importScripts", re.I),
    ("eval / Function", r"\beval\s*\(|\bFunction\s*\(|\bnew\s+Function\b", 0),
    ("code in a timer string", r"\bset(Timeout|Interval)\s*\(\s*['\"`]", 0),
    ("imports", r"\bimport\s*\(|^\s*import\s+[\w{*'\"]|<script[^>]*\btype\s*=\s*['\"]?module", re.I | re.M),
    ("external script", r"<script[^>]*\bsrc\s*=", re.I),
    ("external resources: <link>", r"<link\b", re.I),
    ("embedded frames/objects", r"<(iframe|frame|object|embed|portal)\b|\bsrcdoc\b", re.I),
    ("<base> tag", r"<base\b", re.I),
    ("forms that submit somewhere", r"<form\b[^>]*\baction\s*=", re.I),
    ("auto-redirect", r"http-equiv\s*=\s*['\"]?refresh", re.I),
    ("remote URL", r"\b(https?|wss?|ftp):\s*/\s*/|(src|href|action)\s*=\s*['\"]?\s*//", re.I),
    ("javascript: URL", r"javascript\s*:", re.I),
    ("cookies", r"document\s*\.\s*cookie", 0),
    ("reaching the host page", r"\b(top|parent|opener|frameElement)\s*\.|window\s*\.\s*(top|parent|opener)\b", 0),
    ("raw postMessage (use lantern.notify)", r"postMessage", 0),
    ("storage APIs (use lantern.storage)", r"\b(localStorage|sessionStorage|indexedDB|caches)\b", 0),
    ("obfuscation", r"\b(window|globalThis|self)\s*\[|\.\s*constructor\b|\[\s*['\"`]constructor|\\x[0-9a-fA-F]{2}|\\u[0-9a-fA-F]{4}|\batob\s*\(|fromCharCode", 0),
    ("location changes", r"\blocation\s*(\.\s*(href|assign|replace)|=)", 0),
    ("window.open", r"\bopen\s*\(\s*['\"`]", 0),
]]
WIDGET_EFFORT = "low"
# Health guidance stays out (CLAUDE.md rule 5): a water timer is a timer. Live run, Oct 27: the model
# added a daily "8 glasses" goal and the old pattern missed it, so intake amounts and goals are caught too.
HEALTH = re.compile(r"\b(\w*hydrat\w*|you should drink|litres? (a|per) day|liters? (a|per) day|"
                    r"glasses (a|per) day|\d+\s*(glasses|cups|litres?|liters?|ml|oz)\b|daily (goal|intake)|"
                    r"kidney|diagnos\w*|calorie\w*|medical advice)\b", re.I)

WIDGET_RULES = """You write one small widget for Lantern's desktop panel: a single self-contained HTML document.
Hard rules (a checker enforces them; breaking one means the widget is rejected):
- Inline <style> and <script> only. No external files, fonts, images or URLs of any kind.
- No network: no fetch, XMLHttpRequest, WebSocket, EventSource, sendBeacon, workers.
- No eval, Function, imports, string timers, window.open, cookies, localStorage, location changes.
- Never touch top, parent, opener or postMessage. Use only Lantern's bridge:
    lantern.notify(text)                     show a short message to the user
    await lantern.storage.get(key)           read a saved value (null if none)
    lantern.storage.set(key, value)          save a small JSON value
- Write every character directly: no \\u or \\x escapes, no atob, fromCharCode or .constructor.
- It renders in a 420x340 pixel frame and must fit without scrolling. Under 150 lines.
  Friendly, rounded, warm colours. Readable at a glance.
- No health, medical or nutrition advice of any kind: a water or break timer is a plain countdown,
  with no drinking goals, intake counts, amounts or "stay hydrated" lines. Jokes are fine.
Reply with the HTML document only."""

Approve = Callable[[str, str, dict], Any]
Commit = Callable[[Any, dict], "dict | None"]


class WidgetError(Exception):
    pass


def check(html: str) -> list[str]:
    """Reasons the widget is unsafe (empty list = passes)."""
    problems = []
    if len(html.encode()) > MAX_BYTES:
        problems.append(f"too big ({len(html.encode())} bytes; limit {MAX_BYTES})")
    for why, rx in BANNED:
        if rx.search(html):
            problems.append(why)
    if HEALTH.search(html):
        problems.append("health or medical advice")
    return problems


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")[:41]
    if not NAME.match(s):
        raise WidgetError(f"bad widget name {name!r}")
    return s


def extract_html(text: str) -> str:
    m = re.search(r"```(?:html)?\s*\n(.*?)```", text, re.S | re.I)
    html = (m.group(1) if m else text).strip()
    if "<" not in html:
        raise WidgetError("the model did not return HTML")
    return html


def title_of(html: str, default: str) -> str:
    m = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip()[:60] if m else default


def _complete(client: Any, **kw) -> Any:
    """One widget-writing call, streamed with low reasoning effort.
    Live API, Oct 27: the non-streamed call failed every time after 30 s ("upstream request failed")
    while the model reasoned through ~23k tokens; streamed at effort "low" it takes ~14 s and ~3k
    tokens. Returns the final response (a fake client in tests may return it directly)."""
    result = client.responses.create(stream=True, reasoning={"effort": WIDGET_EFFORT}, **kw)
    if hasattr(result, "output_text"):
        return result
    final = None
    for event in result:
        kind = getattr(event, "type", "")
        if kind == "response.completed":
            final = event.response
        elif kind in ("response.failed", "response.incomplete", "error"):
            raise WidgetError(f"the model couldn't finish the widget ({kind})")
    if final is None:
        raise WidgetError("the widget stream ended without a result")
    return final


@dataclass
class Widgets:
    user_dir: Path
    builtin_dir: Path = REPO_WIDGETS

    def _dirs(self) -> list[tuple[Path, str]]:
        return [(self.builtin_dir, "built-in"), (Path(self.user_dir).expanduser(), "yours")]

    def list(self) -> list[dict]:
        out, seen = [], set()
        for folder, source in reversed(self._dirs()):   # yours win over built-ins with the same name
            if not folder.exists():
                continue
            for f in sorted(folder.glob("*.html")):
                if f.stem in seen:
                    continue
                seen.add(f.stem)
                html = f.read_text(encoding="utf-8", errors="replace")
                out.append({"name": f.stem, "title": title_of(html, f.stem), "source": source,
                            "ok": not check(html)})
        return sorted(out, key=lambda w: w["name"])

    def get(self, name: str) -> str:
        name = slug(name)
        for folder, _ in reversed(self._dirs()):
            f = folder / f"{name}.html"
            if f.exists():
                html = f.read_text(encoding="utf-8", errors="replace")
                if problems := check(html):  # files can change on disk: check every time
                    raise WidgetError(f"{name} failed the safety check: {', '.join(problems)}")
                return html
        raise WidgetError(f"no widget called {name}")

    def generate(self, client: Any, description: str, *, model: str = models.SPARK,
                 on_usage: Callable[[str, models.Usage], None] | None = None) -> tuple[str, int]:
        """Ask the model for the widget; one retry with the checker's findings. Returns (html, attempts)."""
        response = _complete(client, model=models.check_model(model), instructions=WIDGET_RULES,
                             input=f"Make this widget: {description}")
        for attempt in (1, 2):
            if on_usage:
                on_usage(model, models.Usage.from_response(getattr(response, "usage", None)))
            html = extract_html(getattr(response, "output_text", "") or "")
            problems = check(html)
            if not problems:
                return html, attempt
            if attempt == 2:
                raise WidgetError("the widget still failed the safety check: " + ", ".join(problems))
            response = _complete(
                client, model=model, instructions=WIDGET_RULES, previous_response_id=response.id,
                input="The safety checker rejected it for: " + "; ".join(problems)
                      + ". Return the corrected full HTML document only.")
        raise WidgetError("unreachable")

    def save(self, name: str, html: str, *, approve: Approve, commit: Commit) -> dict:
        name = slug(name)
        if problems := check(html):
            raise WidgetError("refusing to save: " + ", ".join(problems))
        payload = {"name": name, "sha256": hashlib.sha256(html.encode()).hexdigest(), "bytes": len(html.encode())}
        ok = approve("save_widget", f"Add widget '{name}' ({payload['bytes']:,} bytes, passed the safety checker). "
                     "It runs sandboxed: no network, no files, only notify and its own storage.", payload)
        if ok is None:
            return {"saved": False, "reason": "user declined"}
        if refused := commit(ok, {"name": name, "sha256": hashlib.sha256(html.encode()).hexdigest(),
                                  "bytes": len(html.encode())}):
            return {"saved": False, "reason": refused["reason"]}
        folder = Path(self.user_dir).expanduser()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{name}.html"
        path.write_text(html, encoding="utf-8")
        verified = hashlib.sha256(path.read_bytes()).hexdigest() == payload["sha256"]
        return {"saved": verified, "verified": verified, "name": name, "path": str(path),
                "title": title_of(html, name), "note": "Open it in the Widgets tab."}
