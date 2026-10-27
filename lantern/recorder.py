"""Teach once by demonstration (S8): the user does a web task once in Lantern's browser; Lantern learns it (S4).

  1. Record. Lantern opens its own browser on the page the user names, with a small recorder in every page
     (forms/driver.py RECORDER_JS). Each page reports its fields when it loads and again whenever the user clicks
     a button or link, plus each field they change. Password fields never report a value, and the values stay on
     this computer: they are kept as local steps in the encrypted memory, like fill_form's traces.
  2. Turn it into traces. Each page the user moved on from becomes one `trace` step in exactly the shape
     fill_form writes (forms/filler.py): the fields they set (with option values for choices), the button they
     pressed, where it led. The last page is checked for a confirmation, as fill_form does. A file the user
     attached is found by name in their folders (never in a private zone).
  3. Learn. The finished teaching task is compiled by S4 (compiler.py) like any verified task: the contract comes
     from these traces, Muse Spark names and describes it (the user's narration helps), and it is verified before
     it's trusted. Nothing about the recording is sent anywhere except that contract, and no screenshots at all.

Fields only the user may fill (password, OTP, card, ID; forms/guards.py) can be part of a demonstration, but then
there is no routine to make: S4 refuses forms with such fields, and the teaching task says why.
This records demonstrations in Lantern's browser only. OS-wide hooks (other apps) aren't built: S4 routines can
only replay web forms (docs/DECISIONS.md).
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .forms import guards
from .forms.filler import _trace_entry
from .policy import PRIVATE, PolicyError

MAX_SECONDS = 30 * 60      # a teaching session ends by itself after this long
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv"}


class TeachError(RuntimeError):
    """The demonstration can't become a routine; the message says why, in the user's words."""


@dataclass
class Recording:
    url: str
    events: list = field(default_factory=list)
    final_url: str = ""
    evidence: str = ""        # the confirmation the last page showed, if any
    seconds: float = 0.0


def record(driver_factory: Callable[[], Any], url: str, *, stop: threading.Event,
           confirm_selectors: tuple[str, ...], act: Callable[[Any], None] | None = None,
           on_event: Callable[[dict], None] | None = None, max_seconds: float = MAX_SECONDS) -> Recording:
    """Open `url` in Lantern's browser and record until `stop` is set, the user closes the page, or time runs out.
    `act` replaces the user (tests, scripted demos): it gets the driver and drives the page itself."""
    rec = Recording(url)
    t0 = time.monotonic()

    def got(event: dict) -> None:
        rec.events.append(event)
        if on_event is not None:
            on_event(event)
    d = driver_factory()
    d.start()
    try:
        d.record(got)
        d.open(url)
        if act is not None:
            act(d)
        else:
            while not stop.is_set() and not d.page.is_closed() and time.monotonic() - t0 < max_seconds:
                d.page.wait_for_timeout(250)   # keeps the browser's events flowing to `got`
        if not d.page.is_closed():
            d.page.wait_for_timeout(300)       # the last page's own events
            rec.final_url = d.url
            rec.evidence = d.confirmation(confirm_selectors)
    finally:
        d.close()
    rec.seconds = round(time.monotonic() - t0, 1)
    return rec


# ---- recorded events -> fill_form traces ------------------------------------------------------------
def _key(f: dict) -> str:
    return f.get("name") or f.get("html_id") or f.get("label") or ""


def _path(url: str) -> str:
    return urlparse(url).path


BACK = re.compile(r"\b(back|previous|prev|cancel)\b|←", re.I)


def _submit_like(click: dict) -> bool:
    """A press that sends the page: a button or submit input that isn't Back/Previous/Cancel."""
    if BACK.search(click.get("text") or ""):
        return False
    if click.get("tag") == "button" or click.get("type") in ("submit", "image"):
        return True
    return bool(click.get("in_form")) and click.get("tag") != "a"


def _set(before: list[dict], after: list[dict], changed: set[str]) -> list[dict]:
    """The fields the user set on a page: changed while they watched, or different from when the page loaded."""
    start = {_key(f): f.get("value", "") for f in before}
    out = []
    for f in after:
        k = _key(f)
        if f.get("type") == "file" and not f.get("value"):
            continue
        if k in changed or (f.get("value", "") != start.get(k, "") and f.get("value", "") != ""):
            out.append(f)
    return out


def find_file(policy, name: str) -> str | None:
    """The one file with this name in the user's folders (a browser only tells a page the file's name)."""
    hits = []
    for root in policy.roots:
        try:
            for full, zone in policy.walk(root, SKIP_DIRS):
                if full.name == name and zone != PRIVATE:
                    hits.append(full)
        except (OSError, PolicyError):
            continue
    if not hits:
        return None
    return str(max(hits, key=lambda p: policy.stat(p).st_mtime))   # the newest copy, as a person would pick


def traces_from(rec: Recording, policy) -> list[dict]:
    """The recording as fill_form traces, one per page the user moved on from. Raises TeachError."""
    pages: list[dict] = []
    cur: dict | None = None
    for ev in rec.events:
        kind = ev.get("kind")
        if kind == "page":
            if cur is not None and cur.get("click") is not None and _path(ev["url"]) != cur["path"]:
                cur["final_url"] = ev["url"]
                _close(pages, cur)
                cur = None
            if cur is None or _path(ev["url"]) != cur["path"]:
                cur = {"url": ev["url"], "path": _path(ev["url"]), "start": ev.get("fields") or [],
                       "changed": set(), "click": None, "fields": []}
            # the same page again (a validation error): keep what was set, wait for the next press
            continue
        if cur is None or _path(ev.get("url", "")) != cur["path"]:
            # the page's own load report can come before the recorder was listening: start it here
            cur = {"url": ev.get("url", ""), "path": _path(ev.get("url", "")), "start": [], "changed": set(),
                   "click": None, "fields": []}
        if kind == "field":
            cur["changed"].add(_key(ev))
        elif kind == "click" and _submit_like(ev):
            cur["click"] = ev
            cur["fields"] = ev.get("fields") or []
    end = cur
    if end is not None and end.get("click") is not None and rec.final_url and _path(rec.final_url) != end["path"]:
        end["final_url"] = rec.final_url
        _close(pages, end)
    if not pages:
        raise TeachError("I didn't see you send any page of the form. Fill it in and press its button, then "
                         "press Done")
    traces = []
    for i, p in enumerate(pages):
        traces.append(_trace(p, policy, last=i == len(pages) - 1, evidence=rec.evidence))
    if not rec.evidence:
        raise TeachError("I didn't see a confirmation at the end, so I can't tell the form went through. Finish "
                         "the last page, wait for its confirmation, then press Done")
    return traces


def _close(pages: list[dict], page: dict) -> None:
    """A page the user sent. Sent again after going back: the new one replaces it and what followed it."""
    for i, p in enumerate(pages):
        if p["path"] == page["path"]:   # what was set the first time still counts (the site kept it)
            page["changed"] |= {_key(f) for f in _set(p["start"], p["fields"], p["changed"])}
            del pages[i:]
            break
    pages.append(page)


def _trace(page: dict, policy, *, last: bool, evidence: str) -> dict:
    fields, left = [], []
    for f in _set(page["start"], page["fields"], page["changed"]):
        if guards.classify(f):
            left.append(f.get("label") or f.get("name") or "a field")
            continue
        value = f.get("value", "")
        if f["type"] == "checkbox":
            value = "yes" if value == "checked" else "no"
        elif f["type"] == "file":
            path = find_file(policy, Path(value.split(",")[0].strip()).name)
            if path is None:
                raise TeachError(f"you attached {value}, but I can't find it in your folders (private ones don't "
                                 "count). Put it in Documents, then teach me again")
            value = path
        elif f["type"] in ("custom", "richtext"):
            raise TeachError(f"'{f.get('label') or f.get('name')}' is a custom control; routines can only replay "
                             "ordinary form fields for now")
        fields.append(_trace_entry(f, value))
    click = page["click"]
    return {"tool": "fill_form", "url": page["url"], "fields": fields, "submit": click.get("text") or "Submit",
            "submitted": True, "final_url": page.get("final_url", ""), "verified": bool(last and evidence),
            "advanced": not last, "evidence": evidence[:300] if last else "", "visual": [],
            "left_for_you": left, "taught": True}
