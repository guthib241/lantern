"""S1 end to end against a real target: the City Permit Portal (demo/portal) in headless Chromium.

Three tasks arrive from the phone at once. The permit task asks which permit type to apply for and parks; the
other two (email the slides, tidy Downloads) finish while it waits. The answer comes back as a button tap, and
the permit task fills all six portal pages with Lantern's form filler and gets a reference number. The models
are scripted (FakeClient per task); the browser, the portal, the files and the approvals are real."""
import hashlib
import json
import shutil
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

from fakes import call
from lantern.channels import ChannelHub, FakeChannel
from lantern.forms import FormService, FormSettings
from lantern.forms.driver import BrowserDriver, chromium_executable
from lantern.policy import Policy
from lantern.core import Core
from test_scheduler import Routed, ask, config, finish, until

PORTAL = Path(__file__).resolve().parents[1] / "demo" / "portal"
SEED = json.loads((PORTAL / "seed.json").read_text(encoding="utf-8"))["applicant"]


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
def portal():
    sys.path.insert(0, str(PORTAL))
    try:
        from server import serve
    finally:
        sys.path.remove(str(PORTAL))
    httpd, app = serve(port=0, background=True)
    yield f"http://127.0.0.1:{httpd.server_address[1]}", app
    httpd.shutdown()
    httpd.server_close()


class OneBrowser:
    """One browser for the whole application, as a person would keep it open: the portal's session cookie
    lives between pages. (Lantern's FormService opens a fresh browser per fill_form call, which loses a
    session cookie; keeping a browser per task for multi-page forms is left to S4/S5, see NEXT_SESSION.)
    Playwright's sync API is bound to the thread that started it, so it opens in the task's own thread."""

    def __init__(self, profile, exe):
        self.profile, self.exe, self.d = profile, exe, None

    def __call__(self):
        return self

    def __enter__(self):
        if self.d is None:
            self.d = BrowserDriver(self.profile, executable_path=self.exe).__enter__()
        return self.d

    def __exit__(self, *exc):
        if "/apply/done" in self.d.page.url or exc[0] is not None:
            self.d.__exit__(None, None, None)
            self.d = None
        return False


def values(cid, **fields):
    return ([call("set_values", cid, values=[{"field": k, "value": v} for k, v in fields.items()])], "")


def fill(cid, url, goal):
    return ([call("fill_form", cid, url=url, goal=goal)], "")


def test_three_tasks_from_the_phone_one_parks_the_others_finish_then_the_portal_is_filled(tmp_path, chromium, portal):
    base, app = portal
    cfg = config(tmp_path)
    docs = Path(cfg.roots[0])
    plan = docs / "site_plan.pdf"
    shutil.copy(PORTAL / "fixtures" / "site_plan.pdf", plan)
    (docs / "Slides").mkdir()
    (docs / "Slides" / "pitch-slides.pdf").write_bytes(b"%PDF-1.4 slides")
    downloads = docs / "Downloads"
    downloads.mkdir()
    for name in ("photo.jpg", "notes.txt"):
        (downloads / name).write_text(name)

    asked = threading.Event()
    s = SEED
    client = Routed({
        "Form fields": [   # the form filler's planning calls, one per page that has fields
            values("v1", full_name=s["full_name"], date_of_birth=s["date_of_birth"], email=s["email"], phone=s["phone"]),
            values("v2", street=s["street"], city=s["city"], pin_code=s["pin_code"]),
            values("v3", permit_type="Home renovation", start_date=s["start_date"], duration_days=s["duration_days"],
                   description=s["description"]),
            values("v4", id_proof_type="Passport", site_plan=str(plan)),
            values("v6", pay_method="counter", declaration="yes"),
        ],
        "permit application": [
            ask("p0", "Which permit type should I apply for?", ["Home renovation", "Street event", "Tree trimming"]),
            fill("p1", f"{base}/apply/personal", "Personal details from the profile"),
            fill("p2", f"{base}/apply/address", "Address from the profile"),
            fill("p3", f"{base}/apply/permit", "Home renovation permit"),
            fill("p4", f"{base}/apply/documents", "Passport, and the site plan file"),
            fill("p5", f"{base}/apply/review", "Review and continue"),
            fill("p6", f"{base}/apply/payment", "Pay at the counter and accept the declaration"),
            finish("p7", "filed the permit application"), ([], "Filed. Reference on the receipt."),
        ],
        "slides": [
            asked,   # starts only once the permit task is parked, so "finished while it waited" is certain
            ([call("search_files", "s1", query="slides")], ""),
            ([call("send_email", "s2", to=["rahul@example.com"], subject="The slides", body="Here you go.",
                   attachments=[str(docs / "Slides" / "pitch-slides.pdf")])], ""),
            finish("s3", "sent Rahul the slides"), ([], "Sent."),
        ],
        "Downloads": [
            asked,
            ([call("organize_files", "t1", moves=[{"source": str(downloads / "photo.jpg"),
                                                   "destination": str(downloads / "Images") + "/"},
                                                  {"source": str(downloads / "notes.txt"),
                                                   "destination": str(downloads / "Text") + "/"}])], ""),
            finish("t2", "tidied Downloads"), ([], "Tidy."),
        ],
    })
    policy = Policy.load(cfg.zones_file, cfg.roots)
    settings = FormSettings(profile_dir=tmp_path / "browser-profile", media_dir=tmp_path / "media",
                            vault_path=tmp_path / "profile.json", confirm_wait_ms=800)
    forms = FormService(settings, None, policy, None, driver_factory=OneBrowser(settings.profile_dir, chromium))
    core = Core(cfg, client, policy=policy, forms=forms, reminder_tick=0, workers=3)
    forms.memory = core.memory
    events = []
    core.subscribe(events.append)
    core.subscribe(lambda e: asked.set() if e["type"] == "task_parked" and e["reason"] == "waiting_user" else None)
    phone = FakeChannel()
    hub = ChannelHub(core, [phone]).start(listen=False)
    hub.question_delay = 0.05

    def tap_approvals(kind, a):   # the owner taps Approve on each card that reaches the phone
        if kind == "requested":
            core.bus.decide(a.id, a.nonce, True, by="fake")
    core.bus.subscribe(tap_approvals)

    for text in ("fill the city permit application", "send Rahul the slides", "tidy my Downloads"):
        hub.receive(phone.incoming(text))
    hub.wait_idle()
    ids = {k: t for k, t in zip(("permit", "slides", "tidy"), sorted(hub.origin))}

    until(lambda: all(core.memory.get_task(ids[k]).status == "done" for k in ("slides", "tidy")), 20)
    assert core.memory.get_task(ids["permit"]).status == "active"
    order = [(e["type"], e.get("task_id") or e.get("id")) for e in events if e["type"] in ("task_parked", "done")]
    assert order.index(("task_parked", ids["permit"])) < order.index(("done", ids["slides"]))
    assert order.index(("task_parked", ids["permit"])) < order.index(("done", ids["tidy"]))
    assert (downloads / "Images" / "photo.jpg").exists() and (downloads / "Text" / "notes.txt").exists()

    until(lambda: any(m.get("buttons") and "permit type" in m["text"] for m in phone.messages()))
    card = next(m for m in phone.messages() if m.get("buttons") and "permit type" in m["text"])
    hub.receive(phone.incoming(button=card["buttons"][0]["id"]))   # "Home renovation"
    hub.wait_idle()
    assert core.wait_idle(120)

    task = core.memory.get_task(ids["permit"])
    assert task.status == "done", task.summary
    with urllib.request.urlopen(f"{base}/api/applications") as r:
        [ref] = json.loads(r.read())["references"]
    with urllib.request.urlopen(f"{base}/api/applications/{ref}") as r:   # the portal's own record: the postcondition
        data = r.read().decode()
    assert "Asha Rao" in data and "home_renovation" in data
    assert hashlib.sha256(plan.read_bytes()).hexdigest() in data          # the right file went up
    last = [json.loads(st["content"]) for st in core.memory.steps(ids["permit"]) if st["kind"] == "tool_result"][-2]
    assert last["name"] == "fill_form" and last["result"]["verified"] is True
    texts = [m["text"] for m in phone.messages()]
    assert "Sent." in texts and "Tidy." in texts and "Filed. Reference on the receipt." in texts
    hub.stop()
    core.shutdown()


def test_the_submit_finder_never_picks_back(tmp_path, chromium):
    """Found by the portal run: with Back and Next both submit buttons, Lantern used to click Back."""
    page = tmp_path / "wizard.html"
    page.write_text("<form><input name=a><button type=submit name=_nav value=back formnovalidate>Back</button>"
                    "<button type=submit name=_nav value=next>Next</button></form>"
                    "<form id=f2><input name=b><button type=submit>← Previous</button>"
                    "<button type=submit>Continue →</button></form>")
    with BrowserDriver(tmp_path / "profile", executable_path=chromium) as d:
        d.open(page.as_uri())
        assert d.find_submit()["label"] == "Next"
