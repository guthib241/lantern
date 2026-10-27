"""M13: widgets: static checker, generation with one retry, approval-bound save, sandbox in a real browser."""
import http.server
import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fakes import FakeClient, call
from types import SimpleNamespace as NS
from lantern.agent import Agent
from lantern.approvals import ApprovalBus, CallbackGate
from lantern.memory import Memory
from lantern.tools import Toolbox, ToolError
from lantern.widgets import REPO_WIDGETS, WidgetError, Widgets, check, extract_html, slug

ROOT = Path(__file__).resolve().parent.parent
WATER = (REPO_WIDGETS / "water-timer.html").read_text()
GOOD = "<!doctype html><title>Counter</title><button id=b>+1</button><script>let n=0;" \
       "b.onclick=()=>{n++;b.textContent=n;lantern.storage.set('n',n)};</script>"


@pytest.mark.parametrize("snippet, why", [
    ("fetch('/x')", "fetch"), ("new XMLHttpRequest()", "XMLHttpRequest"), ("new WebSocket('ws://x')", "WebSocket"),
    ("new EventSource('/s')", "EventSource"), ("navigator.sendBeacon('/b', d)", "sendBeacon"),
    ("new Worker('w.js')", "workers"), ("eval('1')", "eval"), ("new Function('return 1')", "eval"),
    ("setTimeout('alert(1)', 1)", "timer string"), ("import('./x.js')", "imports"),
    ('<script type="module">x</script>', "imports"), ('<script src="a.js"></script>', "external script"),
    ('<link rel="stylesheet" href="s.css">', "<link>"), ("<iframe></iframe>", "frames"),
    ('<img src="https://evil.test/p.gif">', "remote URL"), ('<img src="//evil.test/p.gif">', "remote URL"),
    ('<a href="javascript:alert(1)">x</a>', "javascript"), ("document.cookie", "cookies"),
    ("parent.postMessage(1, '*')", "host page"), ("window.top.location", "host page"),
    ("localStorage.setItem('a', 1)", "storage APIs"), ("window['fe' + 'tch']", "obfuscation"),
    ("[].constructor.constructor('x')()", "obfuscation"), ("'\\x66etch'", "obfuscation"),
    ("atob('ZmV0Y2g=')", "obfuscation"), ("location.href = 'x'", "location"), ("open('https://x')", "remote URL"),
    ('<form action="/send">', "forms"), ('<meta http-equiv="refresh" content="0">', "redirect"),
    ("<p>You should drink 3 litres a day</p>", "health"), ("x" * 100_001, "too big"),
])
def test_checker_rejects(snippet, why):
    problems = check(f"<html><body>{snippet}</body></html>")
    assert any(why.lower() in p.lower() for p in problems), problems


def test_checker_accepts_the_water_timer_and_ordinary_code():
    assert check(WATER) == []
    assert check(GOOD) == []
    assert check("<script>class Timer { constructor(ms) { this.ms = ms } }</script>") == []
    for word in ("hydrate", "doctor", "medical"):
        assert word not in WATER.lower()


def test_extract_and_slug():
    assert extract_html("Here you go:\n```html\n<div>x</div>\n```") == "<div>x</div>"
    with pytest.raises(WidgetError):
        extract_html("sorry, no")
    assert slug("Water Timer!") == "water-timer"
    with pytest.raises(WidgetError):
        slug("!!!")


def kit(tmp_path, client, answer=True):
    mem = Memory(tmp_path / "m.db")
    bus = ApprovalBus()
    asked = []
    tb = Toolbox([tmp_path / "docs"], mem, CallbackGate(lambda s: asked.append(s) or answer, bus),
                 widgets=Widgets(tmp_path / "widgets"))
    tb.client = client
    return tb, bus, asked, mem


def test_make_widget_generates_checks_and_saves(tmp_path):
    (tmp_path / "docs").mkdir()
    client = FakeClient([([], f"```html\n{GOOD}\n```")])
    tb, bus, asked, mem = kit(tmp_path, client)
    r = tb.make_widget("Click counter", "a button that counts clicks")
    assert r["saved"] and r["verified"] and r["name"] == "click-counter" and r["attempts"] == 1
    assert (tmp_path / "widgets" / "click-counter.html").read_text() == GOOD
    assert "passed the safety checker" in asked[0]
    a = list(bus._items.values())[0]
    assert a.action == "save_widget" and len(a.payload["sha256"]) == 64
    assert "no network" in client.requests[0]["instructions"].lower()


def test_one_retry_with_findings_then_refuse(tmp_path):
    (tmp_path / "docs").mkdir()
    client = FakeClient([([], "<script>fetch('https://x')</script>"), ([], GOOD)])
    tb, *_ = kit(tmp_path, client)
    assert tb.make_widget("counter", "count")["attempts"] == 2
    retry = client.requests[1]
    assert retry["previous_response_id"] == "resp_1" and "network: fetch()" in retry["input"]
    bad = FakeClient([([], "<script>fetch('/a')</script>"), ([], "<script>new WebSocket('ws://x')</script>")])
    (tmp_path / "b" / "docs").mkdir(parents=True)
    tb2, *_ = kit(tmp_path / "b", bad)
    with pytest.raises(ToolError, match="still failed"):
        tb2.make_widget("bad", "x")
    assert not (tmp_path / "b" / "widgets").exists()


def test_declined_save_writes_nothing(tmp_path):
    (tmp_path / "docs").mkdir()
    tb, *_ = kit(tmp_path, FakeClient([([], GOOD)]), answer=False)
    assert tb.make_widget("counter", "count")["saved"] is False
    assert not (tmp_path / "widgets").exists()


def test_list_and_serve_with_recheck(tmp_path):
    w = Widgets(tmp_path / "mine")
    assert [x["name"] for x in w.list()] == ["water-timer"] and w.list()[0]["title"] == "Water break timer"
    (tmp_path / "mine").mkdir()
    (tmp_path / "mine" / "evil.html").write_text("<script>fetch('http://evil.test')</script>")
    assert {x["name"]: x["ok"] for x in w.list()}["evil"] is False
    with pytest.raises(WidgetError, match="failed the safety check"):
        w.get("evil")
    assert "Water break timer" in w.get("water-timer")


def test_server_routes(tmp_path):
    from lantern.config import Config
    from lantern.core import Core
    from lantern.server import create_app
    cfg = Config(roots=[str(tmp_path)], db_path=str(tmp_path / "m.db"))
    cfg.audit_path, cfg.zones_file, cfg.widgets_dir = str(tmp_path / "a.db"), str(tmp_path / "z.json"), str(tmp_path / "w")
    core = Core(cfg, FakeClient([]))
    http = TestClient(create_app(core, "t"))
    h = {"X-Lantern-Token": "t"}
    assert http.get("/widgets").status_code == 401
    assert [x["name"] for x in http.get("/widgets", headers=h).json()["widgets"]] == ["water-timer"]
    assert "<title>Water break timer</title>" in http.get("/widgets/water-timer", headers=h).json()["html"]
    assert http.get("/widgets/nope", headers=h).status_code == 404


def test_agent_makes_a_widget(tmp_path):
    (tmp_path / "docs").mkdir()
    make = NS(type="function_call", name="make_widget", call_id="c1",
              arguments=json.dumps({"name": "water timer", "description": "a 45 minute timer"}))
    client = FakeClient([([make], ""),
                         ([], GOOD),
                         ([call("finish", "c2", status="done", summary="made a water timer")], ""),
                         ([], "Your timer is in the Widgets tab.")])
    tb, *_ = kit(tmp_path, client)
    r = Agent(client, tb.memory, tb).run("make me a water timer")
    assert r.status == "done" and (tmp_path / "widgets" / "water-timer.html").exists()
    assert json.loads(client.requests[2]["input"][0]["output"])["model"] == "Muse Spark"


# ---- the sandbox itself, in a real browser ---------------------------------------------------
@pytest.fixture(scope="module")
def browser():
    pw = pytest.importorskip("playwright.sync_api")
    from lantern.forms.driver import chromium_executable
    try:
        p = pw.sync_playwright().start()
        b = p.chromium.launch(executable_path=chromium_executable())
    except Exception as e:
        pytest.skip(f"Chromium not available: {e}")
    yield b
    b.close()
    p.stop()


PARENT = """<!doctype html><iframe id="h" sandbox="allow-scripts" src="HOST"></iframe><script>
window.got = []; window.ready = false;
const h = document.getElementById('h');
h.addEventListener('load', () => { window.ready = true; });
addEventListener('message', (e) => { if (e.source === h.contentWindow) window.got.push(e.data); });
window.load = (html) => h.contentWindow.postMessage({type: 'lantern.load', html}, '*');
window.reply = (payload) => h.contentWindow.postMessage({type: 'lantern.reply', payload}, '*');
</script>"""


def test_widget_host_sandbox_blocks_network_and_host_access(browser, tmp_path):
    hits = []

    class Spy(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass
    server = http.server.HTTPServer(("127.0.0.1", 0), Spy)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    host = (ROOT / "desktop" / "renderer" / "widget-host.html").as_uri()
    (tmp_path / "parent.html").write_text(PARENT.replace("HOST", host))
    page = browser.new_page()
    try:
        page.goto((tmp_path / "parent.html").as_uri())
        page.wait_for_function("window.ready")
        # A widget that got past the checker somehow: it must still be stuck in the sandbox.
        sneaky = f"""<script>
lantern.notify('hello from the widget');
fetch('http://127.0.0.1:{port}/steal').then(() => lantern.notify('NETWORK OK')).catch(() => lantern.notify('network blocked'));
try {{ parent.document.title; lantern.notify('HOST REACHED'); }} catch (e) {{ lantern.notify('host blocked'); }}
try {{ localStorage.setItem('x', 1); lantern.notify('STORAGE OK'); }} catch (e) {{ lantern.notify('storage blocked'); }}
lantern.storage.get('n').then((v) => lantern.notify('stored ' + v));
</script>"""
        page.evaluate("(html) => window.load(html)", sneaky)
        page.wait_for_function("window.got.filter(m => m.type === 'lantern.widget').length >= 4", timeout=8000)
        got = page.evaluate("window.got")
        texts = [m["payload"].get("text") for m in got if m["payload"].get("type") == "notify"]
        assert "hello from the widget" in texts and "host blocked" in texts and "storage blocked" in texts
        get = [m["payload"] for m in got if m["payload"].get("type") == "storage.get"][0]
        page.evaluate("(p) => window.reply(p)", {"id": get["id"], "value": 7})
        page.wait_for_function("window.got.some(m => m.payload.text === 'stored 7')", timeout=5000)
        page.wait_for_timeout(500)
        texts = [m["payload"].get("text") for m in page.evaluate("window.got") if m["payload"].get("type") == "notify"]
        assert "network blocked" in texts and "NETWORK OK" not in texts and "HOST REACHED" not in texts
        assert hits == []
        # And the real pre-made widget renders and talks only through the bridge.
        page.evaluate("(html) => window.load(html)", WATER)
        page.wait_for_function("window.got.some(m => m.payload.type === 'storage.get' && m.payload.key === 'minutes')",
                               timeout=5000)
    finally:
        page.close()
        server.shutdown()


def test_generation_streams_with_low_effort():
    """Live API, Oct 27: unstreamed widget calls died at 30 s; streamed at effort "low" they take ~14 s."""
    from types import SimpleNamespace as NS
    from lantern.widgets import WIDGET_EFFORT, WidgetError, Widgets
    html = "<!doctype html><html><body><p>tick</p><script>let n = 0;</script></body></html>"
    seen = []

    class Streaming:
        def __init__(self, events):
            self.events, self.responses = events, self

        def create(self, **kw):
            seen.append(kw)
            return iter(self.events)
    done = NS(type="response.completed", response=NS(id="r1", output_text=html, usage=None))
    out, attempts = Widgets.generate(Widgets.__new__(Widgets), Streaming([NS(type="response.created"), done]), "a timer")
    assert out.startswith("<!doctype html>") and attempts == 1
    assert seen[0]["stream"] is True and seen[0]["reasoning"] == {"effort": WIDGET_EFFORT} == {"effort": "low"}
    with pytest.raises(WidgetError, match="couldn't finish"):
        Widgets.generate(Widgets.__new__(Widgets), Streaming([NS(type="response.failed")]), "a timer")


@pytest.mark.parametrize("text", ["Goal reached! 8 glasses!", "Stay hydrated, obviously.", "Drink 2 litres", "daily goal: 6 cups"])
def test_intake_goals_count_as_health_advice(text):
    """Live run, Oct 27: a generated water timer set a daily "8 glasses" goal and passed the old pattern."""
    from lantern.widgets import check
    assert "health or medical advice" in check(f"<!doctype html><html><body><p>{text}</p></body></html>")


def test_agent_rules_forbid_health_advice():
    from lantern.agent import BASE_RULES
    assert "Never give health, medical or nutrition advice" in BASE_RULES
