"""M7 (real Chromium via Playwright, scripted fake model): fill, hard stops, approval-bound Submit, verify.

Skipped cleanly when Playwright or Chromium isn't available."""
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace as NS
from urllib.parse import parse_qs, urlparse

import pytest

from fakes import FakeClient, call, click, computer_call, usage
from lantern.approvals import ApprovalBus, CallbackGate
from lantern.audit import AuditLog, GuardedClient
from lantern.forms import FormService, FormSettings, Vault
from lantern.forms.computer import ComputerLoop
from lantern.forms.driver import BrowserDriver, chromium_executable
from lantern.forms.filler import DEMO_FORMS
from lantern.memory import Memory
from lantern.policy import Policy, Rule
from lantern.tools import Toolbox, ToolError

pytest.importorskip("playwright.sync_api")

SIMPLE = """<!doctype html><html><head><title>Sign up</title></head><body style="margin:0;font:16px sans-serif">
<form action="done.html" method="get" style="padding:20px">
 <label for="nick">Nickname</label> <input id="nick" name="nick" required style="width:300px"><br><br>
 <label for="email">Email</label> <input id="email" name="email" type="email" style="width:300px"><br><br>
 <label for="pw">Password</label> <input id="pw" name="pw" type="password" style="width:300px"><br><br>
 <a id="away" href="https://example.com/">elsewhere</a><br><br>
 <button id="go" type="submit">Submit</button>
</form></body></html>"""
DONE = '<!doctype html><title>Done</title><div id="confirmation">Thanks! Reference REF-1234</div>'
CAPTCHA = """<!doctype html><title>Captcha</title><form action="done.html">
<label for="nick">Nickname</label><input id="nick" name="nick">
<div class="g-captcha" style="width:300px;height:80px;background:#eee" id="robot-box">I'm not a robot</div>
<button type="submit">Submit</button></form>"""


@pytest.fixture(scope="module")
def chromium():
    exe = chromium_executable()
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch(executable_path=exe).close()
    except Exception as e:  # no browser in this environment
        pytest.skip(f"Chromium not available: {e}")
    return exe


def write_site(d):
    d.mkdir(exist_ok=True)
    (d / "simple.html").write_text(SIMPLE)
    (d / "done.html").write_text(DONE)
    (d / "captcha.html").write_text(CAPTCHA)
    return d


@pytest.fixture
def site(tmp_path):
    return write_site(tmp_path / "site")


@pytest.fixture(scope="module")
def at(chromium, tmp_path_factory):
    """Pixel centres of the elements the scripted model clicks, measured once in the same
    1280x800 layout Lantern's browser sees: at[page][selector] -> (x, y)."""
    site = write_site(tmp_path_factory.mktemp("measure"))
    wanted = {"simple.html": ["#nick", "#pw", "#go", "#away"], "captcha.html": ["#robot-box"],
              str(DEMO_FORMS / "reimbursement.html"): ["#category"]}
    out = {}
    with BrowserDriver(tmp_path_factory.mktemp("profile"), executable_path=chromium) as d:
        def measure(page, selectors):
            for s in selectors:
                b = d.page.locator(s).first.bounding_box()
                out.setdefault(page, {})[s] = (round(b["x"] + b["width"] / 2), round(b["y"] + b["height"] / 2))
        for page, selectors in wanted.items():
            d.open(Path(page).as_uri() if page.startswith("/") else (site / page).as_uri())
            measure(Path(page).name, selectors)
        d.page.click("#category")
        measure("reimbursement.html", ['li[data-value="travel"]'])
    return out


def settings(tmp_path, demo_dir=DEMO_FORMS, **kw):
    return FormSettings(profile_dir=tmp_path / "browser-profile", media_dir=tmp_path / "media",
                        vault_path=tmp_path / "profile.json", demo_dir=demo_dir, confirm_wait_ms=800, **kw)


def driver(tmp_path, chromium):
    return BrowserDriver(tmp_path / "browser-profile", executable_path=chromium)


def toolbox(tmp_path, client, chromium, answer=True, demo_dir=DEMO_FORMS, root=None, factory=None, **kw):
    root = root or (tmp_path / "docs")
    root.mkdir(exist_ok=True)
    mem = Memory(tmp_path / "m.db")
    bus = ApprovalBus()
    asked = []

    def decide(summary):
        asked.append(summary)
        return answer(summary) if callable(answer) else answer
    policy = Policy.from_roots([root])
    s = settings(tmp_path, demo_dir, **kw)
    forms = FormService(s, client, policy, mem, driver_factory=factory)
    if factory is None:
        forms.driver_factory = lambda: BrowserDriver(s.profile_dir, executable_path=chromium)
    tb = Toolbox(policy, mem, CallbackGate(decide, bus), forms=forms)
    return tb, bus, asked, mem


# ---- the demo form, end to end ---------------------------------------------------------
def test_reimbursement_form_end_to_end(tmp_path, chromium, at):
    combo, travel = at["reimbursement.html"]["#category"], at["reimbursement.html"]['li[data-value="travel"]']
    root = tmp_path / "docs"
    root.mkdir()
    (root / "taxi_receipt.txt").write_text("Taxi, 2400 INR")
    Vault(tmp_path / "profile.json").set("full name", "Asha Rao")
    client = FakeClient([
        ([call("set_values", "p1", values=[
            {"field": "Full name", "value": "Asha Rao"}, {"field": "Email", "value": "asha@example.com"},
            {"field": "Expense category", "value": "Travel"}, {"field": "Expense date", "value": "25/10/2026"},
            {"field": "Amount", "value": "₹2,400"}, {"field": "Currency", "value": "INR"},
            {"field": "Purpose", "value": "Taxi to the venue"},
            {"field": "Receipt (PDF or image)", "value": str(root / "taxi_receipt.txt")},
            {"field": "I confirm these expenses are accurate and were for the hackathon.", "value": "yes"},
            {"field": "Organiser portal password", "value": "hunter2"},   # not offered; must be ignored
        ])], "", usage(900, 0, 80)),
        ([computer_call("c1", click(*combo))], "", usage(2000, 0, 30)),
        ([computer_call("c2", click(*travel))], "", usage(3000, 1500, 30)),
        ([], "Picked Travel.", usage(4000, 3000, 10)),
    ])
    tb, bus, asked, mem = toolbox(tmp_path, client, chromium, root=root)
    events = []
    tb.on_event = lambda kind, data: events.append((kind, data))
    r = tb.fill_form("hackathon reimbursement", "Claim my taxi: 2400 INR on 25 Oct, receipt in Documents")

    assert r["done"] and r["verified"] and r["submitted"], r
    assert "REF-" in r["confirmation"]["content"] and r["confirmation"]["untrusted"]
    assert Path(r["screenshot"]).exists() and Path(r["screenshot"]).parent == tmp_path / "media"
    q = parse_qs(urlparse(r["final_url"]).query, keep_blank_values=True)
    assert q["full_name"] == ["Asha Rao"] and q["category"] == ["travel"] and q["amount"] == ["2400"]
    assert q["expense_date"] == ["2026-10-25"] and q["receipt"] == ["taxi_receipt.txt"]
    for trap in ("portal_password", "otp", "card_number", "cvv", "pan"):
        assert q[trap] == [""], trap
    assert {x["label"] for x in r["left_for_you"]} == {
        "Organiser portal password", "One-time code sent to your phone", "Card number for payout", "CVV", "PAN (tax ID)"}
    # one approval, for Submit, bound to hashes (no raw values in the payload)
    assert len(asked) == 1 and asked[0].startswith("Submit 'Hackathon Reimbursement Request'")
    assert "Left for you: Organiser portal password" in asked[0] and "Asha Rao" in asked[0]
    a = list(bus._items.values())[0]
    assert a.action == "submit_form" and a.status == "used" and "Asha Rao" not in json.dumps(a.payload)
    # the computer-use requests follow the native computer tool shape
    first, second = client.requests[1], client.requests[2]
    assert first["tools"] == [{"type": "computer"}]
    assert first["input"][0]["content"][1]["image_url"].startswith("data:image/jpeg;base64,")
    out = second["input"][0]
    assert out["type"] == "computer_call_output" and out["call_id"] == "c1" and second["previous_response_id"] == "resp_2"
    assert out["output"]["type"] == "computer_screenshot" and out["output"]["image_url"].startswith("data:image/jpeg")
    assert r["model"] == "Muse Spark" and r["steps"] == 3 and r["screenshots"] == 3
    assert tb.tainted
    assert sum(u["calls"] for u in mem.usage_by_model()) == 4
    assert [e for k, e in events if k == "form" and e["phase"] == "step"][0]["badge"] == "Muse Spark"


def test_declined_submit_sends_nothing(tmp_path, chromium, site):
    client = FakeClient([([call("set_values", "p", values=[{"field": "Nickname", "value": "robin"}])], "")])
    tb, bus, asked, _ = toolbox(tmp_path, client, chromium, answer=False, demo_dir=site)
    r = tb.fill_form((site / "simple.html").as_uri(), "sign me up as robin")
    assert r == {**r, "done": False, "submitted": False, "verified": False, "reason": "user declined"}
    assert "final_url" not in r and len(asked) == 1


def test_value_changed_after_approval_is_refused(tmp_path, chromium, site):
    holder = {}

    def factory():
        holder["d"] = BrowserDriver(tmp_path / "browser-profile", executable_path=chromium)
        return holder["d"]

    def sneaky_yes(summary):  # a page script changes a field while the user is approving
        holder["d"].page.fill("#email", "attacker@evil.test")
        return True
    client = FakeClient([([call("set_values", "p", values=[{"field": "Nickname", "value": "robin"},
                                                            {"field": "Email", "value": "robin@example.com"}])], "")])
    tb, bus, asked, _ = toolbox(tmp_path, client, chromium, answer=sneaky_yes, demo_dir=site, factory=factory)
    r = tb.fill_form((site / "simple.html").as_uri(), "sign up")
    assert r["submitted"] is False and r["verified"] is False and "changed after it was approved" in r["reason"]


def test_verification_false_without_confirmation(tmp_path, chromium):
    # Category (custom widget) is never chosen, so the page's own validation blocks the submit.
    client = FakeClient([([call("set_values", "p", values=[
        {"field": "Full name", "value": "Asha"}, {"field": "Email", "value": "a@example.com"},
        {"field": "Expense date", "value": "2026-10-25"}, {"field": "Amount", "value": "10"},
        {"field": "Purpose", "value": "tea"},
        {"field": "I confirm these expenses are accurate and were for the hackathon.", "value": "yes"}])], "")])
    tb, *_ = toolbox(tmp_path, client, chromium)
    r = tb.fill_form("demo reimbursement", "claim tea")
    assert r["submitted"] and not r["verified"] and not r["done"] and "no confirmation" in r["reason"]


def test_captcha_means_you_do_this_part(tmp_path, chromium):
    client = FakeClient([([call("set_values", "p", values=[{"field": "Email", "value": "a@example.com"}])], "")])
    tb, bus, asked, _ = toolbox(tmp_path, client, chromium)
    r = tb.fill_form("demo traps", "verify my account")
    assert r["needs_user"] and "CAPTCHA" in r["reason"] and not r["submitted"] and asked == []
    plan_input = client.requests[0]["input"]
    for secret_field in ("Password", "Aadhaar", "Card number", "UPI PIN", "PAN"):
        assert f'"label": "{secret_field}' not in plan_input   # never offered to the model
    assert {"Password", "Aadhaar number", "UPI PIN", "Secure card payment (stripe)"} <= {x["label"] for x in r["left_for_you"]}


def test_disallowed_url_and_private_upload(tmp_path, chromium, site):
    tb, *_ = toolbox(tmp_path, FakeClient([]), chromium, demo_dir=site)
    with pytest.raises(ToolError, match="isn't on the list"):
        tb.fill_form("https://example.com/form", "x")
    with pytest.raises(ToolError, match="demo folder"):
        tb.fill_form("file:///etc/hosts", "x")

    root = tmp_path / "docs"
    (root / "Private").mkdir(parents=True)
    (root / "Private" / "salary.txt").write_text("secret")
    html = SIMPLE.replace('<button', '<input type="file" id="f" name="f" aria-label="Attachment"><button')
    (site / "upload.html").write_text(html)
    client = FakeClient([([call("set_values", "p", values=[
        {"field": "Nickname", "value": "robin"}, {"field": "Attachment", "value": str(root / "Private" / "salary.txt")}])], "")])
    tb, *_ = toolbox(tmp_path, client, chromium, answer=False, demo_dir=site, root=root)
    tb.policy = Policy([Rule.make(root, "writable"), Rule.make(root / "Private", "private")])
    tb.forms.policy = tb.policy
    r = tb.fill_form((site / "upload.html").as_uri(), "attach my salary file")
    assert r["problems"] and "private" in r["problems"][0]["problem"]


# ---- the computer-use loop's hard stops ----------------------------------------------------
def loop_on(tmp_path, chromium, site, page, script, **kw):
    d = driver(tmp_path, chromium)
    d.start()
    d.open((site / page).as_uri())
    client = FakeClient(script)
    return d, client, ComputerLoop(client, d, **kw)


def test_typing_into_password_is_refused(tmp_path, chromium, site, at):
    nick, pw = at["simple.html"]["#nick"], at["simple.html"]["#pw"]
    d, client, loop = loop_on(tmp_path, chromium, site, "simple.html", [
        ([computer_call("c1", click(*nick), {"type": "type", "text": "robin"})], ""),
        ([computer_call("c2", click(*pw), {"type": "type", "text": "hunter2"})], ""),
    ])
    try:
        r = loop.run("fill the form")
        assert r.status == "hard_stop" and "password field" in r.message
        assert r.refused == [{"action": "type 7 characters", "reason": "password field"}]
        assert d.page.input_value("#nick") == "robin" and d.page.input_value("#pw") == ""
        assert len(client.requests) == 2   # nothing more after the stop
    finally:
        d.close()


def test_card_number_text_refused_even_in_a_plain_field(tmp_path, chromium, site, at):
    nick = at["simple.html"]["#nick"]
    d, client, loop = loop_on(tmp_path, chromium, site, "simple.html", [
        ([computer_call("c1", click(*nick), {"type": "type", "text": "4111 1111 1111 1111"})], "")])
    try:
        r = loop.run("x")
        assert r.status == "hard_stop" and "card number" in r.message and d.page.input_value("#nick") == ""
    finally:
        d.close()


def test_captcha_click_stops(tmp_path, chromium, site, at):
    box = at["captcha.html"]["#robot-box"]
    d, client, loop = loop_on(tmp_path, chromium, site, "captcha.html", [([computer_call("c1", click(*box))], "")])
    try:
        r = loop.run("x")
        assert r.status == "hard_stop" and r.captcha and "you do this part" in r.message
    finally:
        d.close()


def test_model_clicking_submit_never_submits(tmp_path, chromium, site, at):
    url = (site / "simple.html").as_uri()
    nick, go = at["simple.html"]["#nick"], at["simple.html"]["#go"]
    d, client, loop = loop_on(tmp_path, chromium, site, "simple.html", [
        ([computer_call("c1", click(*nick), {"type": "type", "text": "robin"})], ""),
        ([computer_call("c2", click(*go))], ""),
    ])
    try:
        r = loop.run("x")
        assert r.status == "submit_requested" and d.url == url
    finally:
        d.close()


def test_enter_in_a_field_is_blocked_by_the_page_guard(tmp_path, chromium, site, at):
    url = (site / "simple.html").as_uri()
    nick = at["simple.html"]["#nick"]
    d, client, loop = loop_on(tmp_path, chromium, site, "simple.html", [
        ([computer_call("c1", click(*nick), {"type": "type", "text": "robin"}, {"type": "keypress", "keys": ["ENTER"]})], "")])
    try:
        r = loop.run("x")
        assert r.status == "submit_requested" and d.url == url and d.blocked_submits() == 1
    finally:
        d.close()


def test_leaving_allowed_sites_stops(tmp_path, chromium, site, at):
    away = at["simple.html"]["#away"]
    d, client, loop = loop_on(tmp_path, chromium, site, "simple.html", [([computer_call("c1", click(*away))], "")],
                              url_guard=lambda u: None if u.startswith("file:") else "not allowed")
    d.page.route("https://example.com/**", lambda route: route.fulfill(body="<p>elsewhere</p>", content_type="text/html"))
    try:
        r = loop.run("x")
        assert r.status == "hard_stop" and "left the allowed sites" in r.message
    finally:
        d.close()


# ---- safety checks, protocols, limits -----------------------------------------------------
def test_safety_checks_need_approval_and_are_acknowledged(tmp_path, chromium, site, at):
    nick = at["simple.html"]["#nick"]
    check = {"id": "sc_1", "code": "sensitive_domain", "message": "This page asks for personal data."}
    seen = []
    script = [([computer_call("c1", click(*nick), checks=[check])], ""), ([], "done")]
    d, client, loop = loop_on(tmp_path, chromium, site, "simple.html", script,
                              approve_safety=lambda checks, actions: seen.append((checks, actions)) or True)
    try:
        r = loop.run("x")
        assert r.status == "done" and seen[0][0][0]["id"] == "sc_1"
        assert client.requests[1]["input"][0]["acknowledged_safety_checks"] == [check]
    finally:
        d.close()

    d, client, loop = loop_on(tmp_path, chromium, site, "simple.html",
                              [([computer_call("c1", click(*nick), {"type": "type", "text": "x"}, checks=[check])], "")],
                              approve_safety=lambda checks, actions: False)
    try:
        r = loop.run("x")
        assert r.status == "declined" and d.page.input_value("#nick") == "" and len(client.requests) == 1
    finally:
        d.close()


def test_function_protocol_with_normalized_coordinates(tmp_path, chromium, site, at):
    nick = at["simple.html"]["#nick"]
    norm = [round(nick[0] * 1000 / 1280), round(nick[1] * 1000 / 800)]
    d, client, loop = loop_on(tmp_path, chromium, site, "simple.html", [
        ([call("computer.computer", "f1", action="left_click", coordinate=norm)], ""),
        ([call("computer.computer", "f2", action="type", text="robin")], ""),
        ([call("computer.stop", "f3", answer="Typed the nickname.")], ""),
    ], protocol="function")
    try:
        r = loop.run("type robin")
        assert r.status == "done" and r.message == "Typed the nickname." and d.page.input_value("#nick") == "robin"
        req = client.requests[1]
        assert [t["name"] for t in client.requests[0]["tools"]] == ["computer.computer", "computer.stop"]
        assert req["parallel_tool_calls"] is False and req["input"][0]["type"] == "function_call_output"
        assert req["input"][0]["output"][-1]["image_url"].startswith("data:image/jpeg;base64,")
    finally:
        d.close()


class RejectsNativeTool:
    """First call fails like an API that doesn't know the native computer tool."""

    def __init__(self, inner):
        self.inner, self.responses, self.calls = inner, self, 0

    def create(self, **kw):
        self.calls += 1
        if kw.get("tools") == [{"type": "computer"}]:
            err = RuntimeError("unknown tool type 'computer'")
            err.status_code = 400
            raise err
        return self.inner.create(**kw)


def test_native_rejected_falls_back_to_function_tools(tmp_path, chromium, site):
    fake = FakeClient([([call("computer.stop", "f1", answer="nothing to do")], "")])
    d = driver(tmp_path, chromium)
    with d:
        d.open((site / "simple.html").as_uri())
        r = ComputerLoop(RejectsNativeTool(fake), d).run("x")
    assert r.status == "done" and r.protocol == "function"


def test_step_cap_and_cancel(tmp_path, chromium, site):
    script = [([computer_call(f"c{i}", {"type": "wait", "ms": 10})], "") for i in range(5)]
    d, client, loop = loop_on(tmp_path, chromium, site, "simple.html", script, max_steps=2)
    try:
        assert loop.run("x").status == "max_steps" and len(client.requests) == 3
    finally:
        d.close()
    cancel = threading.Event()
    cancel.set()
    d, client, loop = loop_on(tmp_path, chromium, site, "simple.html", script, cancel=cancel)
    try:
        assert loop.run("x").status == "cancelled"
    finally:
        d.close()


def test_screenshots_reach_the_cloud_unscrubbed_and_audited(tmp_path, chromium, site):
    audit = AuditLog(tmp_path / "audit.db")
    fake = FakeClient([([], "nothing to do")])
    d = driver(tmp_path, chromium)
    with d:
        d.open((site / "simple.html").as_uri())
        ComputerLoop(GuardedClient(fake, audit), d).run("x")
    sent = fake.requests[0]["input"][0]["content"][1]["image_url"]
    import base64
    assert base64.b64decode(sent.split(",", 1)[1])[:2] == b"\xff\xd8"   # still a valid JPEG
    assert audit.summary()["cloud"]["bytes"] > 5_000


# ---- through the server: approval on the desktop / phone -----------------------------------
def test_fill_form_task_with_http_approval(tmp_path, chromium, site):
    from fastapi.testclient import TestClient
    from lantern.config import Config
    from lantern.core import Core
    from lantern.server import create_app

    root = tmp_path / "docs"
    root.mkdir()
    cfg = Config(roots=[str(root)], db_path=str(tmp_path / "m.db"))
    cfg.audit_path, cfg.zones_file = str(tmp_path / "a.db"), str(tmp_path / "z.json")
    url = (site / "simple.html").as_uri()
    client = FakeClient([
        ([call("fill_form", "a1", url=url, goal="sign me up as robin")], "", usage(500, 0, 20)),
        ([call("set_values", "p", values=[{"field": "Nickname", "value": "robin"}])], "", usage(300, 0, 20)),
        ([call("finish", "a2", status="done", summary="signed up, confirmation verified")], ""),
        ([], "Done. You're robin now."),
    ])
    s = settings(tmp_path, site)
    core = Core(cfg, client, approval_timeout=10)
    core.forms.settings = s
    core.forms.driver_factory = lambda: BrowserDriver(s.profile_dir, executable_path=chromium)
    http = TestClient(create_app(core, "t"))
    h = {"X-Lantern-Token": "t"}
    tid = http.post("/task", json={"goal": "sign me up"}, headers=h).json()["task_id"]
    pending = []
    for _ in range(1000):
        pending = http.get("/approvals", headers=h).json()
        if pending:
            break
        time.sleep(0.02)
    assert pending and pending[0]["action"] == "submit_form" and "Nickname: robin" in pending[0]["summary"]
    a = pending[0]
    http.post(f"/approvals/{a['id']}", json={"nonce": a["nonce"], "approve": True, "expected_hash": a["payload_hash"]},
              headers=h)
    assert core.wait_idle(30)
    task = http.get(f"/tasks/{tid}", headers=h).json()
    assert task["status"] == "done"
    result = [json.loads(x["content"]) for x in task["steps"] if x["kind"] == "tool_result"][0]["result"]
    assert result["verified"] and "REF-1234" in result["confirmation"]["content"]
    assert task["usage"]["calls"] == 4   # agent calls + the form planner, all on this task
    core.shutdown()
