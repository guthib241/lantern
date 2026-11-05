"""Shared helpers for tests/parallel: an HTTP client with cookies and multipart uploads, local servers for the
portal and the board, and a Chromium that skips cleanly when no browser is installed. Never touches the network
beyond 127.0.0.1, and never calls any model."""
from __future__ import annotations

import functools
import http.cookiejar
import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PORTAL = REPO / "demo" / "portal"
ATTACKS = REPO / "demo" / "attacks"
BOARD = REPO / "desktop" / "board"
sys.path.insert(0, str(PORTAL))

from portal import LABELS, REFERENCE  # noqa: E402
from server import serve  # noqa: E402

__all__ = ["REPO", "PORTAL", "ATTACKS", "BOARD", "LABELS", "REFERENCE", "SEED", "SITE_PLAN", "Client", "portal_server",
           "names", "fill_application", "static_server", "browser", "until", "serve"]

SEED = json.loads((PORTAL / "seed.json").read_text(encoding="utf-8"))["applicant"]
SITE_PLAN = PORTAL / "fixtures" / "site_plan.pdf"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    """A tiny browser: keeps cookies, posts forms (urlencoded or multipart), optionally follows redirects."""

    def __init__(self, base: str):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.follow = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.stay = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar), _NoRedirect)

    def request(self, method: str, path: str, fields: dict | None = None, files: dict | None = None,
                follow: bool = True) -> tuple[int, dict, str, str]:
        data, headers = None, {}
        if files:
            boundary = uuid.uuid4().hex
            parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
                     for k, v in (fields or {}).items()]
            for name, (filename, content) in files.items():
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                             f'Content-Type: application/octet-stream\r\n\r\n'.encode() + content + b"\r\n")
            data = b"".join(parts) + f"--{boundary}--\r\n".encode()
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        elif fields is not None:
            data = urllib.parse.urlencode(fields).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        opener = self.follow if follow else self.stay
        try:
            with opener.open(req, timeout=10) as r:
                return r.status, dict(r.headers), r.read().decode("utf-8"), r.url
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read().decode("utf-8"), e.url

    def get(self, path: str, **kw):
        return self.request("GET", path, **kw)

    def post(self, path: str, fields: dict | None = None, **kw):
        return self.request("POST", path, fields=fields or {}, **kw)

    def json(self, path: str) -> dict:
        return json.loads(self.get(path)[2])


@contextmanager
def portal_server(layout: str = "v1", attacks: bool = False):
    httpd, portal = serve(port=0, layout=layout, attacks=attacks, background=True)
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", portal
    finally:
        httpd.shutdown()
        httpd.server_close()


def names(layout: str) -> dict:
    return {k: v[0] for k, v in LABELS[layout].items()}


def fill_application(c: Client, layout: str = "v1", pay: dict | None = None, upload: tuple | None = None) -> tuple[int, str, str]:
    """Walk the whole form with the seeded applicant. Returns (status, final url, final page)."""
    n = names(layout)
    steps = [("personal", ["full_name", "date_of_birth", "email", "phone"]), ("address", ["street", "city", "pin_code"]),
             ("permit", ["permit_type", "start_date", "duration_days", "description"])]
    for step, keys in steps:
        status, _, body, url = c.post(f"/apply/{step}", {**{n[k]: SEED[k] for k in keys}, "_nav": "next"})
        assert status == 200 and "/apply/" in url and f"/apply/{step}" not in url, (step, status, body[:500])
    upload = upload or (SITE_PLAN.name, SITE_PLAN.read_bytes())
    status, _, body, url = c.post("/apply/documents", {n["id_proof_type"]: SEED["id_proof_type"], "_nav": "next"},
                                  files={n["site_plan"]: upload})
    assert url.endswith("/apply/review"), body[:800]
    status, _, body, url = c.post("/apply/review", {"_nav": "next"})
    assert url.endswith("/apply/payment"), body[:500]
    fields = {n["pay_method"]: "counter", n["declaration"]: "yes", "_nav": "next"}
    fields.update({n[k]: v for k, v in (pay or {}).items()})
    status, _, body, url = c.post("/apply/payment", fields)
    return status, url, body


@contextmanager
def static_server(directory: Path):
    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(directory)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture(scope="module")
def browser():
    """Chromium through Playwright, or a skip. Uses Playwright's own browser, else the sandbox's /opt/pw-browsers.
    Module scope: each test module imports this fixture, and only one sync Playwright may run at a time."""
    sync_api = pytest.importorskip("playwright.sync_api")
    pw = sync_api.sync_playwright().start()
    b = None
    for kwargs in ({}, {"executable_path": "/opt/pw-browsers/chromium"}):
        try:
            b = pw.chromium.launch(**kwargs)
            break
        except Exception:
            continue
    if b is None:
        pw.stop()
        pytest.skip("no Chromium available for Playwright")
    yield b
    b.close()
    pw.stop()


def until(page, js: str, timeout: float = 60.0):
    """Poll a JS expression through Page.evaluate (works under the board's strict CSP, unlike wait_for_function)."""
    end = time.time() + timeout
    while time.time() < end:
        value = page.evaluate(js)
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for: {js}")
