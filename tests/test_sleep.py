"""S11: Sleep. Overnight the portal is redesigned (v2); the nightly check replays the learned routine on a sandbox copy
(nothing real happens), heals the moved labels, verifies v2, and the morning digest reaches the phone; the
morning's run then works first time with zero model calls. Plus suggestions, the schedule and a routine that needs
the user. Every model is scripted."""
import json
import shutil
import sys
import time
from pathlib import Path

import pytest

from lantern import sleep as sleep_mod
from lantern.channels import ChannelHub, FakeChannel
from lantern.core import Core
from lantern.forms import FormService, FormSettings
from lantern.forms.driver import BrowserDriver
from lantern.forms.vault import Vault
from lantern.policy import Policy
import test_relearn
from test_relearn import agent_run, form_values
from test_routines import PORTAL, PROFILE, TRACES, Both, Chat, code_answer, contract_answer
from test_scheduler import Routed, config, until

chromium = test_relearn.chromium


@pytest.fixture
def portal():
    sys.path.insert(0, str(PORTAL))
    try:
        import portal as pm
        from server import serve
    finally:
        sys.path.remove(str(PORTAL))
    servers = []

    def start(layout="v1"):
        httpd, app = serve(port=0, layout=layout, background=True)
        servers.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}", app
    yield start, pm
    for h in servers:
        h.shutdown()
        h.server_close()


def portal_core(tmp_path, chromium, start, pm, repair_works=True):
    base, app = start("v1")
    cfg = config(tmp_path)
    plan = Path(cfg.roots[0]) / "site_plan.pdf"
    shutil.copy(PORTAL / "fixtures" / "site_plan.pdf", plan)
    pages = ["personal", "address", "permit", "documents", "review", "payment"]
    routed = Routed({"Form fields": form_values(plan), "city permit": agent_run(base, plan, pages)})
    v1_to_v2 = {pm.LABELS["v1"][k][1]: pm.LABELS["v2"][k][1] for k in pm.LABELS["v1"]}
    v1_to_v2.update({pm.BUTTONS["v1"][k][1]: pm.BUTTONS["v2"][k][1] for k in pm.BUTTONS["v1"]})

    def repair(kw):   # knows "Full name" and "Applicant's name" are the same thing (or, broken, knows nothing)
        q = json.loads(kw["messages"][1]["content"])
        listed = json.loads(next(v for k, v in q.items() if k.startswith("page_"))["content"])
        want = v1_to_v2.get(q["used_to_be"], "").lower() if repair_works else ""
        return {"target": next((c["label"] for c in listed if c["label"].lower() == want), "none")}
    chat = Chat(routine_contract=contract_answer, routine_code=code_answer, routine_repair=repair)
    policy = Policy.load(cfg.zones_file, cfg.roots)
    settings = FormSettings(profile_dir=tmp_path / "browser-profile", media_dir=tmp_path / "media",
                            vault_path=tmp_path / "profile.json", confirm_wait_ms=800)
    Vault(settings.vault_path).save({"profile": PROFILE, "answers": {}, "forms": {}})
    forms = FormService(settings, None, policy, None,
                        driver_factory=lambda: BrowserDriver(settings.profile_dir, executable_path=chromium))
    core = Core(cfg, Both(routed, chat), policy=policy, forms=forms, reminder_tick=0, workers=2)
    forms.memory = core.memory
    core.compiler.sandbox = lambda b: start(app.default_layout)[0]     # a copy of the site as it is now
    core.bus.subscribe(lambda kind, a: core.bus.decide(a.id, a.nonce, True, by="test") if kind == "requested" else None)
    first = core.submit("fill the city permit application")
    assert core.wait_idle(120) and core.memory.get_task(first).status == "done"
    core.learn(first)
    assert core.wait_idle(120) and core.routines()[0]["trusted"]
    return core, app, chat


def test_overnight_the_drifted_portal_is_healed_on_a_copy_and_the_morning_run_just_works(tmp_path, chromium, portal):
    start, pm = portal
    core, app, chat = portal_core(tmp_path, chromium, start, pm)
    phone = FakeChannel()
    hub = ChannelHub(core, [phone]).start(listen=False)
    app.default_layout = "v2"                                          # the city redesigns the portal overnight
    before = len(app.applications)
    night = core.sleep()
    assert core.wait_idle(240)
    t = core.memory.get_task(night)
    assert t.status == "done" and "the page had changed" in t.summary and "now v2" in t.summary, t.summary
    assert "on a copy of each site" in t.summary
    assert len(app.applications) == before                             # nothing real happened overnight
    r = core.compiler.store.load("city-permit-application")
    assert r.version == 2 and r.trusted and "Applicant's name" in r.code
    until(lambda: any(m["text"].startswith("🌙") for m in phone.messages()), 10)   # the digest reached the phone
    repairs = len(chat.of("routine_repair"))
    morning = core.submit("fill the city permit application")         # first try, no healing needed
    assert core.wait_idle(120)
    m = core.memory.get_task(morning)
    assert m.status == "done" and "0 model calls" in m.summary and "healed" not in m.summary, m.summary
    assert len(chat.of("routine_repair")) == repairs and len(app.applications) == before + 1
    hub.stop()
    core.shutdown()


def test_a_routine_that_cant_be_repaired_needs_you_in_the_morning(tmp_path, chromium, portal):
    start, pm = portal
    core, app, _chat = portal_core(tmp_path, chromium, start, pm, repair_works=False)
    app.default_layout = "v2"
    night = core.sleep()
    assert core.wait_idle(240)
    t = core.memory.get_task(night)
    assert "needs you" in t.summary and core.compiler.store.load("city-permit-application").version == 1, t.summary
    core.shutdown()


def test_suggestions_and_the_schedule(tmp_path, monkeypatch):
    core = Core(config(tmp_path), Routed({}), reminder_tick=0, workers=1)
    for _ in range(2):                                                 # done twice, never learned
        t = core.memory.start_task("[whatsapp] fill the city permit application")
        for tr in TRACES:
            core.memory.log_step(t.id, "trace", tr)
        core.memory.finish_task(t.id, "done", "filed")
    s = sleep_mod.suggestions(core.compiler, core.memory)
    assert s["learn"] == [{"goal": "fill the city permit application", "times": 2, "task_id": t.id}]
    skel = core.compiler.skeleton(t.id)
    for name in ("permit-a", "permit-b"):                              # the same steps under two names
        core.compiler.store.save({**skel, "name": name}, "", {"ok": True}, how="test", trusted=True)
    assert sleep_mod.suggestions(core.compiler, core.memory)["merge"] == [["permit-a", "permit-b"]]
    text = sleep_mod.digest({"checked": [], "learn": s["learn"], "merge": [["permit-a", "permit-b"]]})
    assert f"learn {t.id}" in text and "could be one routine" in text

    lt = time.localtime()
    at = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 3, 30, 0, 0, 0, -1))
    assert sleep_mod.due(core.memory, at + 60, {"LANTERN_SLEEP_AT": "03:30"})
    assert not sleep_mod.due(core.memory, at - 60, {"LANTERN_SLEEP_AT": "03:30"})
    assert not sleep_mod.due(core.memory, at + 60, {"LANTERN_SLEEP_AT": "off"})
    assert not sleep_mod.due(core.memory, at + 60, {})                 # off unless `lantern --serve` turns it on
    assert sleep_mod.sleep_at({"LANTERN_SLEEP_AT": "25:99"}) is None
    core.memory.start_task(sleep_mod.GOAL)                             # tonight's already ran
    assert not sleep_mod.due(core.memory, time.time() + 1, {"LANTERN_SLEEP_AT": time.strftime("%H:%M")})
    core.shutdown()
