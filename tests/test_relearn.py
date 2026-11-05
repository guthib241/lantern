"""S5: when the site's flow changes (demo/portal flow b adds a Contact page), a learned routine can't heal its way
through. It stops before the final submit, the agent does the task step by step, and that run is re-learned as the
routine's next version, verified on a fresh copy of the changed site. The next run is back at zero model calls.
Every model is scripted; the browser, the portal and the files are real."""
import json
import shutil
import sys
from pathlib import Path

import pytest

from lantern.compiler import before_final_submit
from lantern.core import Core
from lantern.forms import FormService, FormSettings
from lantern.forms.driver import BrowserDriver, chromium_executable
from lantern.forms.vault import Vault
from lantern.policy import Policy
from lantern.routines.runner import RunResult
from test_routines import PORTAL, PROFILE, SEED, Both, Chat, code_answer, contract_answer, fill, values
from test_scheduler import Routed, config, finish


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
def portals():
    sys.path.insert(0, str(PORTAL))
    try:
        from server import serve
    finally:
        sys.path.remove(str(PORTAL))
    servers = []

    def start(flow="a"):
        httpd, app = serve(port=0, background=True, flow=flow)
        servers.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}", app
    yield start
    for h in servers:
        h.shutdown()
        h.server_close()


def agent_run(base, plan, pages):
    return ([fill(f"p{i}", f"{base}/apply/{p}", f"the {p} page") for i, p in enumerate(pages, 1)]
            + [finish("p9", "filed the permit application"), ([], "Filed.")])


def form_values(plan, contact=False):
    s = SEED
    out = [values("v1", full_name=s["full_name"], date_of_birth=s["date_of_birth"], email=s["email"], phone=s["phone"]),
           values("v2", street=s["street"], city=s["city"], pin_code=s["pin_code"])]
    if contact:
        out.append(values("vc", contact_pref="Email"))
    return out + [values("v3", permit_type="Home renovation", start_date=s["start_date"], duration_days=s["duration_days"],
                         description=s["description"]),
                  values("v4", id_proof_type="Passport", site_plan=str(plan)),
                  values("v6", pay_method="counter", declaration="yes")]


def test_a_changed_flow_is_relearned_and_runs_at_zero_again(tmp_path, chromium, portals):
    base, app = portals("a")
    cfg = config(tmp_path)
    plan = Path(cfg.roots[0]) / "site_plan.pdf"
    shutil.copy(PORTAL / "fixtures" / "site_plan.pdf", plan)
    goal = "fill the city permit application"
    a_pages = ["personal", "address", "permit", "documents", "review", "payment"]
    b_pages = ["personal", "address", "contact", "permit", "documents", "review", "payment"]
    responses = Routed({"Form fields": form_values(plan) + form_values(plan, contact=True),
                        "city permit": agent_run(base, plan, a_pages) + agent_run(base, plan, b_pages)})
    chat = Chat(routine_contract=contract_answer, routine_code=code_answer, routine_repair=lambda kw: {"target": "none"})
    policy = Policy.load(cfg.zones_file, cfg.roots)
    settings = FormSettings(profile_dir=tmp_path / "browser-profile", media_dir=tmp_path / "media",
                            vault_path=tmp_path / "profile.json", confirm_wait_ms=800)
    Vault(settings.vault_path).save({"profile": PROFILE, "answers": {}, "forms": {}})
    forms = FormService(settings, None, policy, None,
                        driver_factory=lambda: BrowserDriver(settings.profile_dir, executable_path=chromium))
    core = Core(cfg, Both(responses, chat), policy=policy, forms=forms, reminder_tick=0, workers=2)
    forms.memory = core.memory
    core.compiler.sandbox = lambda b: portals(app.default_flow)[0]      # a fresh copy of the site as it is now
    events, approvals = [], []
    core.subscribe(events.append)

    def tap(kind, a):
        if kind == "requested":
            approvals.append(a.action)
            core.bus.decide(a.id, a.nonce, True, by="test")
    core.bus.subscribe(tap)

    first = core.submit(goal)                                          # 1. the agent, then learn it
    assert core.wait_idle(120) and core.memory.get_task(first).status == "done"
    core.learn(first)
    assert core.wait_idle(120)
    [row] = core.routines()
    assert row["trusted"] and row["version"] == 1

    app.default_flow = "b"                                             # 2. the city adds a Contact page
    second = core.submit(goal)
    assert core.wait_idle(240)
    t2 = core.memory.get_task(second)
    assert t2.status == "done", t2.summary
    assert any(e["type"] == "routine_fallback" for e in events) and any(e["type"] == "routine_relearn" for e in events)
    assert len(app.applications) == 2                                  # one application: the routine never submitted
    stored = list(app.applications.values())[-1]["applicant"]
    assert stored["contact_pref"] == "email"
    r = core.compiler.store.load(row["name"])
    assert r.version == 2 and r.trusted, core.compiler.store.meta(row["name"])["versions"]
    assert "Contact" in json.dumps(r.contract) or "contact_pref" in json.dumps(r.contract)
    meta = core.compiler.store.meta(row["name"])
    assert "flow changed" in meta["versions"][-1]["how"]
    relearn_job = next(t for t in core.memory.recent_tasks(10) if t.goal.startswith(f"Re-learn {row['name']}"))
    assert relearn_job.status == "done" and "Re-learned" in relearn_job.summary

    approvals.clear()                                                  # 3. back at zero model calls
    third = core.submit(goal)
    assert core.wait_idle(120)
    t3 = core.memory.get_task(third)
    assert t3.status == "done" and "0 model calls" in t3.summary, t3.summary
    assert core.memory.task_usage(third)["calls"] == 0 and approvals == ["run_routine"]
    assert len(app.applications) == 3
    core.shutdown()


def test_only_a_stop_before_the_last_submit_falls_back():
    contract = {"kind": "web", "steps": [
        {"id": "s1", "action": "browser.open"}, {"id": "s2", "action": "browser.fill"},
        {"id": "s3", "action": "browser.submit"}, {"id": "s4", "action": "browser.open"},
        {"id": "s5", "action": "browser.submit"}]}

    def stopped_at(step):
        res = RunResult(ok=False, mode="live")
        res.acted = True
        res.failed = type("F", (), {"step_id": step, "why": "x"})()
        return res
    assert before_final_submit(contract, stopped_at("s4"))             # after a Next, before the real submit
    assert not before_final_submit(contract, stopped_at("s5"))         # at the last submit: it may have been pressed
    files = {"kind": "files", "steps": [{"id": "s1", "action": "files.move"}, {"id": "s2", "action": "files.move"}]}
    assert not before_final_submit(files, stopped_at("s1"))            # a moved file is never "nothing happened"
