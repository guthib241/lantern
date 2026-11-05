"""S8: teach once by demonstration. A simulated person fills demo/portal in Lantern's browser while it records; the
recording becomes fill_form-style traces; S4 learns, verifies and reruns it with no model calls. Also: what a
recording keeps (never a password's value), what makes it unusable, and STOP. Every model is scripted."""
import json
import shutil
import threading
from pathlib import Path

import pytest

from lantern.core import Core
from lantern.forms import FormService, FormSettings
from lantern.forms.driver import BrowserDriver
from lantern.forms.vault import Vault
from lantern.policy import PRIVATE, WRITABLE, Policy, Rule
from lantern.recorder import Recording, TeachError, traces_from
from lantern.forms.driver import chromium_executable
from test_routines import PORTAL, PROFILE, SEED, Both, Chat, code_answer, contract_answer
from test_scheduler import Routed, config, until

BASE = "http://127.0.0.1:8770"


def ev(kind, path, **kw):
    return {"kind": kind, "url": BASE + path, **kw}


def f(name, value, type="text", label=None, **kw):
    return {"label": label or name.replace("_", " ").title(), "name": name, "html_id": name, "type": type,
            "value": value, **kw}


@pytest.fixture(scope="module")
def chromium():
    exe = chromium_executable()
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch(executable_path=exe).close()
    except Exception as e:   # no browser in this environment
        pytest.skip(f"Chromium not available: {e}")
    return exe


@pytest.fixture
def portal_mod():
    import sys
    sys.path.insert(0, str(PORTAL))
    try:
        from server import serve
    finally:
        sys.path.remove(str(PORTAL))
    servers = []

    def start(layout="v1"):
        httpd, app = serve(port=0, layout=layout, background=True)
        servers.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}", app
    yield start, None
    for h in servers:
        h.shutdown()
        h.server_close()


@pytest.fixture
def policy(tmp_path):
    docs, priv = tmp_path / "docs", tmp_path / "docs" / "Private"
    priv.mkdir(parents=True)
    (docs / "site_plan.pdf").write_bytes(b"%PDF-1.4 plan")
    (priv / "id_scan.pdf").write_bytes(b"%PDF-1.4 private")
    return Policy([Rule.make(priv, PRIVATE), Rule.make(docs, WRITABLE)])


# ---- recorded events -> traces --------------------------------------------------------------------
def test_a_recording_becomes_the_same_traces_fill_form_writes(policy, tmp_path):
    choice = [{"value": "home_renovation", "text": "Home renovation (₹500)"}, {"value": "street_event", "text": "Street event"}]
    events = [
        ev("page", "/apply/personal", fields=[f("full_name", ""), f("email", "")]),
        ev("field", "/apply/personal", name="full_name"),
        ev("click", "/apply/personal", tag="a", text="Help", href="/help"),              # not a submit
        ev("click", "/apply/personal", tag="button", text="Next", type="submit", in_form=True,
           fields=[f("full_name", "Asha Rao"), f("email", "asha.rao@example.com")]),
        ev("page", "/apply/permit", fields=[f("permit_type", "", "select", options=choice), f("plan", "", "file")]),
        ev("click", "/apply/permit", tag="button", text="← Back", in_form=True, fields=[]),   # went back...
        ev("page", "/apply/personal", fields=[f("full_name", "Asha Rao"), f("email", "asha.rao@example.com")]),
        ev("click", "/apply/personal", tag="button", text="Next", in_form=True,             # ...and sent it again
           fields=[f("full_name", "Asha Rao"), f("email", "asha@example.com")]),
        ev("page", "/apply/permit", fields=[f("permit_type", "", "select", options=choice), f("plan", "", "file")]),
        ev("click", "/apply/permit", tag="button", text="Submit application", in_form=True,
           fields=[f("permit_type", "Home renovation (₹500)", "select", options=choice),
                   f("plan", "site_plan.pdf", "file")]),
        ev("page", "/apply/done?ref=CPP-2026-AB12CD", fields=[]),
    ]
    rec = Recording(BASE + "/apply/personal", events, BASE + "/apply/done?ref=CPP-2026-AB12CD",
                    "Application received. Reference CPP-2026-AB12CD")
    first, last = traces_from(rec, policy)
    assert first["fields"] == [f("full_name", "Asha Rao"), f("email", "asha@example.com")]   # the second send wins
    assert first["submit"] == "Next" and first["final_url"].endswith("/apply/permit") and not first["verified"]
    assert last["fields"][0]["value"] == "home_renovation"                                # the option's own value
    assert last["fields"][1]["value"] == str(tmp_path / "docs" / "site_plan.pdf")         # found by name
    assert last["verified"] and last["evidence"].endswith("CPP-2026-AB12CD") and last["submit"] == "Submit application"
    assert {"submitted", "advanced", "visual", "left_for_you"} <= set(first)


def test_what_a_recording_never_keeps_and_when_it_cant_be_used(policy):
    done = ev("page", "/done", fields=[])
    login = [ev("page", "/login", fields=[f("user", ""), f("password", "", "password", label="Password")]),
             ev("click", "/login", tag="button", text="Sign in", in_form=True,
                fields=[f("user", "asha"), f("password", "•", "password", label="Password")]), done]
    rec = Recording(BASE + "/login", login, BASE + "/done", "Welcome back, reference AB-1")
    [t] = traces_from(rec, policy)
    assert t["left_for_you"] == ["Password"] and all(x["name"] != "password" for x in t["fields"])
    assert "hunter" not in json.dumps(t)
    with pytest.raises(TeachError, match="confirmation"):
        traces_from(Recording(BASE + "/login", login, BASE + "/done", ""), policy)
    with pytest.raises(TeachError, match="didn't see you send"):
        traces_from(Recording(BASE + "/a", [ev("page", "/a", fields=[f("x", "")])], BASE + "/a", "ok"), policy)
    private = [ev("page", "/docs", fields=[f("scan", "", "file")]),
               ev("click", "/docs", tag="button", text="Upload", in_form=True, fields=[f("scan", "id_scan.pdf", "file")]),
               done]
    with pytest.raises(TeachError, match="can't find it"):                 # a private file is never picked up
        traces_from(Recording(BASE + "/docs", private, BASE + "/done", "Reference AB-2"), policy)


def test_teach_refuses_what_it_cannot_do(tmp_path):
    core = Core(config(tmp_path), Routed({}), reminder_tick=0)          # no scheduler
    with pytest.raises(ValueError, match="scheduler"):
        core.teach(BASE + "/apply", "apply")
    core.shutdown()
    (tmp_path / "b").mkdir()
    core = Core(config(tmp_path / "b"), Routed({}), reminder_tick=0, workers=1)
    for url, goal, why in [("file:///etc/passwd", "x", "http"), (BASE, " ", "say what"),
                           ("https://bank.example/transfer", "pay", "LANTERN_FORMS_DOMAINS|allow")]:
        with pytest.raises(ValueError, match=why):
            core.teach(url, goal)
    assert core.memory.recent_tasks(5) == []
    core.shutdown()


# ---- the whole story in Chromium ----------------------------------------------------------------------
def fill_the_portal(core, plan: Path):
    """A person filling the portal by hand (Playwright plays the person; the recorder doesn't know)."""
    s = SEED

    def act(d):
        p = d.page

        def press(sel="#next"):
            with p.expect_navigation():
                p.click(sel)
        p.fill("#full_name", s["full_name"])
        p.fill("#date_of_birth", s["date_of_birth"])
        p.fill("#email", s["email"])
        p.fill("#phone", s["phone"])
        press()
        p.fill("#street", s["street"])
        p.fill("#city", s["city"])
        p.fill("#pin_code", s["pin_code"])
        press()
        core.teach_note(None, "The description of the work is what changes each time.")
        p.select_option("#permit_type", "home_renovation")
        p.fill("#start_date", s["start_date"])
        p.fill("#duration_days", s["duration_days"])
        p.fill("#description", s["description"])
        press()
        p.select_option("#id_proof_type", "passport")
        p.set_input_files("#site_plan", str(plan))
        press()
        press("#to-payment")
        p.check("#declaration")
        press("#submit")
    return act


def test_teach_the_portal_once_then_it_runs_with_no_model_calls(tmp_path, chromium, portal_mod):
    start, _pm = portal_mod
    base, app = start("v1")
    cfg = config(tmp_path)
    plan = Path(cfg.roots[0]) / "site_plan.pdf"
    shutil.copy(PORTAL / "fixtures" / "site_plan.pdf", plan)
    chat = Chat(routine_contract=contract_answer, routine_code=code_answer)
    responses = Routed({})                                              # no agent turns at all
    policy = Policy.load(cfg.zones_file, cfg.roots)
    settings = FormSettings(profile_dir=tmp_path / "browser-profile", media_dir=tmp_path / "media",
                            vault_path=tmp_path / "profile.json", confirm_wait_ms=800)
    Vault(settings.vault_path).save({"profile": PROFILE, "answers": {}, "forms": {}})
    forms = FormService(settings, None, policy, None,
                        driver_factory=lambda: BrowserDriver(settings.profile_dir, executable_path=chromium))
    core = Core(cfg, Both(responses, chat), policy=policy, forms=forms, reminder_tick=0, workers=2)
    forms.memory = core.memory
    core.compiler.sandbox = lambda b: start("v1")[0]                   # verify on a fresh portal
    events, approvals = [], []
    core.subscribe(events.append)

    def tap(kind, a):
        if kind == "requested":
            approvals.append(a.action)
            core.bus.decide(a.id, a.nonce, True, by="test")
    core.bus.subscribe(tap)

    goal = "fill the city permit application"
    teach = core.teach(f"{base}/apply/personal", goal, act=fill_the_portal(core, plan),
                       driver_factory=lambda: BrowserDriver(tmp_path / "teach-profile", executable_path=chromium,
                                                            guard_submits=False))
    assert core.wait_idle(120)
    t = core.memory.get_task(teach)
    assert t.status == "done" and "6 pages" in t.summary, t.summary
    assert len(app.applications) == 1                                   # the person's own application went in
    phases = [e["phase"] for e in events if e["type"] == "teach"]
    assert phases[0] == "recording" and "event" in phases and "stopped" in phases
    assert all("value" not in e for e in events if e["type"] == "teach")   # the panel never gets the values

    [row] = core.routines()                                             # learned by itself, and verified
    assert row["name"] == "city-permit-application" and row["trusted"], row
    [contract_call] = chat.of("routine_contract")
    shown = json.loads(contract_call["messages"][1]["content"])
    assert shown["narration"] == ["The description of the work is what changes each time."]
    assert "{{vault.full_name}}" in json.dumps(shown) and SEED["full_name"] not in json.dumps(shown)
    sent = json.dumps(chat.requests, default=str)
    assert "input_image" not in sent and "image_url" not in sent             # no screenshots, anywhere

    approvals.clear()
    again = core.submit(goal)                                           # the same goal: the routine, at rung 0
    assert core.wait_idle(120)
    t2 = core.memory.get_task(again)
    assert t2.status == "done" and "0 model calls" in t2.summary, t2.summary
    assert core.memory.task_usage(again)["calls"] == 0 and approvals == ["run_routine"]
    assert len(app.applications) == 2
    core.shutdown()


def test_stop_while_teaching_keeps_nothing(tmp_path, chromium, portal_mod):
    start, _pm = portal_mod
    base, _app = start("v1")
    core = Core(config(tmp_path), Routed({}), reminder_tick=0, workers=1)
    watching = threading.Event()
    stopped = threading.Event()

    def act(d):
        d.page.fill("#full_name", "Asha Rao")
        watching.set()
        stopped.wait(10)
    teach = core.teach(f"{base}/apply/personal", "fill the city permit application", act=act,
                       driver_factory=lambda: BrowserDriver(tmp_path / "p", executable_path=chromium,
                                                            guard_submits=False))
    until(watching.is_set, 30)
    with pytest.raises(ValueError, match="already watching"):
        core.teach(f"{base}/apply/personal", "something else")
    core.stop()
    stopped.set()
    assert core.wait_idle(30)
    t = core.memory.get_task(teach)
    assert t.status == "cancelled" and "nothing was kept" in t.summary, t.summary
    assert [s for s in core.memory.steps(teach) if s["kind"] == "trace"] == [] and core.routines() == []
    core.shutdown()


def test_the_teach_routes(tmp_path):
    from fastapi.testclient import TestClient
    from lantern.server import create_app
    core = Core(config(tmp_path), Routed({}), reminder_tick=0, workers=1)
    c, h = TestClient(create_app(core, "t" * 32)), {"X-Lantern-Token": "t" * 32}
    assert c.post("/teach", json={"url": "file:///etc/passwd", "goal": "x"}, headers=h).status_code == 409
    assert c.post("/teach", json={"url": BASE, "goal": "x"}).status_code == 401
    assert c.post("/teach/stop", json={}, headers=h).json() == {"stopped": False}     # nothing being recorded
    assert c.post("/teach/note", json={"text": "hi"}, headers=h).json() == {"noted": False}
    core.shutdown()
