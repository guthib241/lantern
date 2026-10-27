"""S4: self-compiling routines. Compile a fake successful run; the static checker blocks what it must; dry runs
pass and fail; a broken step heals into a new version. Plus the whole story against demo/portal in Chromium:
learn a filled application, run it again with no model calls, flip the portal's layout, heal, run at $0 again.
Every model is scripted (FakeClient / a scripted chat client); nothing reaches the real API."""
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from fakes import call
from lantern.compiler import format_routines
from lantern.core import Core
from lantern.forms import FormService, FormSettings
from lantern.forms.driver import BrowserDriver, chromium_executable
from lantern.forms.vault import Vault
from lantern.policy import Policy
from lantern.receipt import as_text, build
from lantern.routines.checker import check
from lantern.routines.contract import same_plan, skeleton_from_traces, validate
from lantern.routines.runner import execute, load, plan_for, render, same_calls
from lantern.routines.runtime import RoutineContext
from lantern.routines.store import Routine, RoutineStore
from test_scheduler import Routed, config, finish, until

PORTAL = Path(__file__).resolve().parents[1] / "demo" / "portal"
SEED = json.loads((PORTAL / "seed.json").read_text(encoding="utf-8"))["applicant"]
PROFILE = {"full name": SEED["full_name"], "email": SEED["email"], "phone": SEED["phone"],
           "date of birth": SEED["date_of_birth"], "street": SEED["street"], "city": SEED["city"],
           "pin code": SEED["pin_code"]}


# ---- a scripted chat model for the compiler's structured calls ----------------------------------
class Chat:
    """chat.completions: answers by the response_format's schema name; records every request."""

    def __init__(self, **handlers):
        self.handlers = handlers
        self.requests = []
        self.completions = self

    def create(self, **kw):
        self.requests.append(kw)
        name = kw["response_format"]["json_schema"]["name"]
        data = self.handlers[name](kw)
        usage = NS(prompt_tokens=200, completion_tokens=40, prompt_tokens_details=NS(cached_tokens=0),
                   completion_tokens_details=NS(reasoning_tokens=10))
        return NS(choices=[NS(message=NS(content=json.dumps(data)))], usage=usage)

    def of(self, name):
        return [r for r in self.requests if r["response_format"]["json_schema"]["name"] == name]


class Both:
    """One client with the Responses API (the agent) and Chat Completions (the compiler)."""

    def __init__(self, responses, chat):
        self.responses, self.chat = responses, chat


def contract_answer(kw):
    skel = json.loads(kw["messages"][1]["content"])
    desc = next((s["id"] for s in skel["steps"] if s["action"] == "browser.fill"
                 and "Describe" in s["args"]["field"]), None)
    return {"name": "city-permit-application", "title": "City permit application",
            "examples": ["apply for a city permit", "file the permit application on the portal"],
            "steps": [{"id": s["id"], "say": s["say"] + " (portal)"} for s in skel["steps"]],
            "new_params": [{"step": desc, "name": "work_description", "say": "What the work is"}] if desc else []}


def code_answer(kw):
    """A model that writes exactly the contract's code (what a good answer looks like)."""
    contract = json.loads(kw["messages"][1]["content"].split("\n\nYour last answer")[0])
    return {"code": render(contract)}


# ---- unit: the contract ------------------------------------------------------------------------------
TRACES = [
    {"tool": "fill_form", "url": "http://127.0.0.1:8770/apply/personal", "submit": "Next", "submitted": True,
     "final_url": "http://127.0.0.1:8770/apply/address", "verified": False, "advanced": True, "evidence": "",
     "visual": [], "left_for_you": [],
     "fields": [{"label": "Full name", "name": "full_name", "html_id": "full_name", "type": "text", "value": "Asha Rao"},
                {"label": "Describe the work", "name": "description", "html_id": "description", "type": "textarea",
                 "value": "Rebuild the wall"}]},
    {"tool": "fill_form", "url": "http://127.0.0.1:8770/apply/address", "submit": "Submit application",
     "submitted": True, "final_url": "http://127.0.0.1:8770/apply/done?ref=CPP-2026-000001", "verified": True,
     "evidence": "Application received\nReference CPP-2026-000001", "visual": [], "left_for_you": [],
     "fields": [{"label": "Permit type", "name": "permit_type", "html_id": "permit_type", "type": "select",
                 "value": "home_renovation", "options": [{"value": "home_renovation", "text": "Home renovation (₹500)"},
                                                         {"value": "street_event", "text": "Street event (₹1200)"}]}]},
]


def test_a_successful_run_compiles_into_a_contract_with_vault_placeholders_and_params():
    c = skeleton_from_traces("[whatsapp] fill the city permit application", TRACES,
                             {"full name": "Asha Rao"}, {"task_id": 7, "cost_usd": 0.04})
    assert validate(c) == []
    assert c["examples"] == ["fill the city permit application"]
    fill = [s for s in c["steps"] if s["action"] == "browser.fill"]
    assert fill[0]["args"]["value"] == "{{vault.full_name}}"               # never the literal name
    assert fill[2]["args"]["value"] == "{{permit_type}}"
    permit = next(p for p in c["params"] if p["name"] == "permit_type")
    assert permit["values"] == ["home_renovation", "street_event"] and permit["default"] == "home_renovation"
    assert [s["action"] for s in c["steps"]] == ["browser.open", "browser.fill", "browser.fill", "browser.submit",
                                                 "browser.fill", "browser.submit"]
    assert c["success"][0] == {"check": "browser.text_matches", "args": {"pattern": r"CPP\-[A-Za-z0-9]+\-[A-Za-z0-9]+"}}


def test_a_model_may_rename_and_parameterize_but_never_change_the_plan():
    skel = skeleton_from_traces("fill the city permit application", TRACES, {}, {})
    worse = json.loads(json.dumps(skel))
    worse["steps"].insert(3, {"id": "x", "say": "also pay", "action": "browser.submit", "args": {"button": "Pay now"},
                              "post": []})
    assert same_plan(worse, skel)
    retarget = json.loads(json.dumps(skel))
    retarget["steps"][0]["args"]["url"] = "{{base_url}}/admin"
    assert same_plan(retarget, skel)
    bad_value = json.loads(json.dumps(skel))
    bad_value["steps"][1]["args"]["value"] = "Mallory"
    assert same_plan(bad_value, skel)
    literal = json.loads(json.dumps(skel))
    literal["steps"][1]["args"]["value"] = "4111 1111 1111 1111"
    assert any("looks private" in p for p in validate(literal))


# ---- unit: the static checker -------------------------------------------------------------------------
@pytest.mark.parametrize("bad, why", [
    ("import os\ndef run(params):\n    pass\n", "only import allowed"),
    ("import subprocess\ndef run(params):\n    pass\n", "only import allowed"),
    ("from lantern.routines.runtime import browser\nimport socket\ndef run(params):\n    pass\n", "only import"),
    ("def run(params):\n    eval('1')\n", "only the runtime's functions"),
    ("def run(params):\n    exec('import os')\n", "only the runtime's functions"),
    ("def run(params):\n    open('/etc/passwd')\n", "only the runtime's functions"),
    ("from lantern.routines.runtime import browser\ndef run(params):\n    browser.__class__\n", "only calls"),
    ("from lantern.routines.runtime import browser\ndef run(params):\n    browser.page.goto('x')\n",
     "only the runtime's functions"),
    ("from lantern.routines.runtime import browser\ndef run(params):\n    for i in [1]:\n        browser.open('u')\n",
     "`For` isn't allowed"),
    ("from lantern.routines.runtime import browser\ndef run(params):\n    x = params\n", "`Assign` isn't allowed"),
    ("from lantern.routines.runtime import browser\ndef run(params):\n    if browser.text():\n        pass\n",
     "may only compare params"),
    ("from lantern.routines.runtime import browser as b\ndef run(params):\n    pass\n", "only import"),
    ("def run(params, extra):\n    pass\n", "exactly `def run(params):`"),
    ("def helper():\n    pass\ndef run(params):\n    pass\n", "exactly one function"),
])
def test_the_static_checker_blocks_everything_but_the_runtime(bad, why):
    problems = check(bad)
    assert problems and any(why in p for p in problems), problems


def test_checked_code_runs_with_no_builtins():
    calls = []
    names = {"browser": NS(open=lambda url: calls.append(url)), "check": NS(), "files": NS(), "vault": NS(),
             "uia": NS(), "step": None}
    from contextlib import nullcontext
    names["step"] = lambda *a: nullcontext()
    run = load('from lantern.routines.runtime import browser, step\n\ndef run(params):\n'
               '    with step("s1", "open"):\n        browser.open(params["base_url"] + "/x")\n', names)
    run({"base_url": "http://h"})
    assert calls == ["http://h/x"]
    assert run.__globals__["__builtins__"].keys() == {"__import__"}


def test_model_code_must_make_exactly_the_contracts_calls():
    c = skeleton_from_traces("fill the city permit application", TRACES, {}, {})
    good = render(c)
    assert same_calls(good, c) == []
    sneaky = good.replace("        browser.submit('Next')\n", "        browser.submit('Next')\n"
                                                              "        browser.submit('Pay now')\n")
    assert check(sneaky) == [] and same_calls(sneaky, c)                # passes the checker, still refused


# ---- unit: the runtime with a stand-in browser ----------------------------------------------------------
class Page:
    """Two-page wizard: /a (Name, Next) -> /b (Done text)."""

    def __init__(self, layout="v1"):
        self.layout, self.url, self.values, self.clicks = layout, "", {}, []

    def evaluate(self, js, *a):
        if "data-lantern-btn" in js:
            label = {"v1": "Next", "v2": "Continue"}[self.layout] if self.url.endswith("/a") else ""
            return [{"i": 0, "label": label, "disabled": False, "submit": True, "in_form": True}] if label else []
        return None

    def locator(self, sel):
        page = self

        class L:
            first = NS(click=lambda: (page.clicks.append(sel), setattr(page, "url", page.url.replace("/a", "/b"))))
        return L()

    def inner_text(self, sel):
        return "All done. Reference AB-123" if self.url.endswith("/b") else "Form"


class Driver:
    def __init__(self, page):
        self.page = page

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    @property
    def url(self):
        return self.page.url

    def open(self, url):
        self.page.url = url

    def discover(self):
        label = {"v1": "Name", "v2": "Your name"}[self.page.layout]
        fields = [{"id": "f1", "label": label, "name": "n_" + self.page.layout, "html_id": "", "type": "text",
                   "value": self.page.values.get(label, ""), "selector": "#f1"}] if self.page.url.endswith("/a") else []
        return {"fields": fields, "frames": []}

    def set_value(self, f, value):
        self.page.values[f["label"]] = value
        return True


MINI = {"name": "mini-form", "title": "Mini form", "kind": "web", "examples": ["send the mini form"],
        "params": [{"name": "base_url", "type": "url", "default": "http://127.0.0.1:9"}],
        "steps": [{"id": "s1", "say": "Open", "action": "browser.open", "args": {"url": "{{base_url}}/a"},
                   "post": [{"check": "browser.url_contains", "args": {"text": "/a"}}]},
                  {"id": "s2", "say": "Fill Name", "action": "browser.fill",
                   "args": {"field": "Name", "value": "{{vault.full_name}}", "name": "n_v1"},
                   "post": [{"check": "browser.field_equals", "args": {"field": "Name", "value": "{{vault.full_name}}"}}]},
                  {"id": "s3", "say": "Press Next", "action": "browser.submit", "args": {"button": "Next"},
                   "post": [{"check": "browser.url_contains", "args": {"text": "/b"}}]}],
        "success": [{"check": "browser.text_matches", "args": {"pattern": r"AB-\d+"}}]}


def mini(layout="v1", mode="live", **kw):
    page = Page(layout)
    r = Routine("mini-form", 1, MINI, render(MINI), {})
    ctx = RoutineContext(mode=mode, vault={"full name": "Asha Rao"}, driver_factory=lambda: Driver(page), **kw)
    return r, ctx, page


def test_dry_run_passes_up_to_the_first_submit_and_sends_nothing():
    r, ctx, page = mini(mode="dry")
    res = execute(r, {"base_url": "http://127.0.0.1:9"}, ctx)
    assert res.ok and res.stopped_at == "s3" and page.clicks == [] and not res.acted
    assert [x["id"] for x in res.log] == ["s1", "s2", "s3"] and res.log[-1]["dry_skipped"]


def test_dry_run_fails_when_a_step_doesnt_fit_the_page():
    r, ctx, page = mini(layout="v2", mode="dry")                            # no heal hook: nothing to fix it with
    res = execute(r, {"base_url": "http://127.0.0.1:9"}, ctx)
    assert not res.ok and res.failed.step_id == "s2" and "no field labelled 'Name'" in res.failed.why


def test_a_live_run_needs_one_approval_and_stays_inside_it():
    asked, used = [], []

    def approve(action, summary, payload):
        asked.append((action, summary, payload))
        return lambda final: used.append(final)
    r, ctx, page = mini(approve=approve)
    params = {"base_url": "http://127.0.0.1:9"}
    ctx.plan = plan_for(r, params, {"full name": "Asha Rao"}, target="127.0.0.1:9")
    res = execute(r, params, ctx)
    assert res.ok and res.acted and len(asked) == 1 and len(used) == 1
    action, summary, payload = asked[0]
    assert action == "run_routine" and "Name: Asha Rao" in summary and "no model calls" in summary
    assert payload["red"] == [["s3", "browser.submit"]] and "Asha Rao" not in json.dumps(payload["fields"])
    # the same run with a value that differs from the approved one: refused before the submit
    r2, ctx2, page2 = mini(approve=approve)
    ctx2.plan = plan_for(r2, params, {"full name": "Somebody Else"})
    res2 = execute(r2, params, ctx2)
    assert not res2.ok and res2.failed.kind == "refused" and page2.clicks == []


def test_a_declined_run_does_nothing():
    r, ctx, page = mini(approve=lambda *a: None)
    ctx.plan = plan_for(r, {"base_url": "http://127.0.0.1:9"}, {"full name": "Asha Rao"})
    res = execute(r, {"base_url": "http://127.0.0.1:9"}, ctx)
    assert res.declined and not res.ok and page.url == ""


def test_hard_stop_fields_are_never_filled():
    page = Page()
    c = json.loads(json.dumps(MINI))
    c["steps"][1]["args"]["value"] = "4111 1111 1111 1111"
    r = Routine("mini-form", 1, c, render(c), {})
    ctx = RoutineContext(mode="sandbox", driver_factory=lambda: Driver(page))
    res = execute(r, {"base_url": "http://127.0.0.1:9"}, ctx)
    assert res.failed.kind == "hard_stop" and page.values == {}


def test_a_moved_field_heals_by_retargeting_never_by_changing_the_value():
    seen = []

    def heal(step_id, kind, old, candidates):
        seen.append((step_id, kind, old, [c["label"] for c in candidates]))
        return {"field": "Your name", "button": "Continue"}[kind]
    r, ctx, page = mini(layout="v2", mode="sandbox", heal=heal)
    res = execute(r, {"base_url": "http://127.0.0.1:9"}, ctx)
    assert res.ok, res.failed and res.failed.why
    assert seen[0] == ("s2", "field", "Name", ["Your name"]) and seen[1][:3] == ("s3", "button", "Next")
    assert res.patches == {"s2": {"field": "Your name", "name": "n_v2", "html_id": ""}, "s3": {"button": "Continue"}}
    assert page.values == {"Your name": "Asha Rao"}


# ---- unit: the store ---------------------------------------------------------------------------------------
def test_versions_runs_and_saved_cost_come_from_measured_numbers(tmp_path):
    store = RoutineStore(tmp_path / "routines")
    c = dict(MINI, source={"task_id": 3, "cost_usd": 0.05})
    r1 = store.save(c, render(c), {"mode": "sandbox", "ok": True}, how="learned", trusted=True)
    store.record_run("mini-form", {"version": 1, "ok": True, "cost_usd": 0.0, "seconds": 2.0})
    store.record_run("mini-form", {"version": 1, "ok": True, "cost_usd": 0.01, "seconds": 3.0})
    r2 = store.save(c, render(c), {"mode": "sandbox", "ok": True}, how="healed", trusted=True, healed_from=1,
                    patches={"s2": {"field": "Your name"}})
    s = store.summary("mini-form")
    assert (r1.version, r2.version, s["version"], s["runs"], s["heals"]) == (1, 2, 2, 2, 1)
    assert s["saved_usd"] == pytest.approx(0.09) and s["learned_from"] == 3
    untrusted = store.save(c, render(c), {"mode": "dry", "ok": False}, how="healed", trusted=False, healed_from=2)
    assert untrusted.version == 3 and store.meta("mini-form")["current"] == 2   # an unverified heal never takes over
    assert "mini-form" in format_routines([store.summary("mini-form")])


# ---- the whole story on the demo portal -------------------------------------------------------------------
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
    sys.path.insert(0, str(PORTAL))
    try:
        import portal as portal_module
        from server import serve
    finally:
        sys.path.remove(str(PORTAL))
    servers = []

    def start(layout="v1"):
        httpd, app = serve(port=0, layout=layout, background=True)
        servers.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}", app
    yield start, portal_module
    for h in servers:
        h.shutdown()
        h.server_close()


def values(cid, **fields):
    return ([call("set_values", cid, values=[{"field": k, "value": v} for k, v in fields.items()])], "")


def fill(cid, url, goal):
    return ([call("fill_form", cid, url=url, goal=goal)], "")


def test_learn_the_portal_run_it_at_zero_flip_the_layout_heal_and_run_at_zero_again(tmp_path, chromium, portal_mod):
    start, pm = portal_mod
    base, app = start("v1")
    cfg = config(tmp_path)
    docs = Path(cfg.roots[0])
    plan = docs / "site_plan.pdf"
    shutil.copy(PORTAL / "fixtures" / "site_plan.pdf", plan)
    s = SEED
    goal = "fill the city permit application"
    responses = Routed({
        "Form fields": [
            values("v1", full_name=s["full_name"], date_of_birth=s["date_of_birth"], email=s["email"], phone=s["phone"]),
            values("v2", street=s["street"], city=s["city"], pin_code=s["pin_code"]),
            values("v3", permit_type="Home renovation", start_date=s["start_date"], duration_days=s["duration_days"],
                   description=s["description"]),
            values("v4", id_proof_type="Passport", site_plan=str(plan)),
            values("v6", pay_method="counter", declaration="yes"),
        ],
        "city permit": [
            fill("p1", f"{base}/apply/personal", "Personal details"), fill("p2", f"{base}/apply/address", "Address"),
            fill("p3", f"{base}/apply/permit", "Home renovation permit"),
            fill("p4", f"{base}/apply/documents", "Passport and the site plan"),
            fill("p5", f"{base}/apply/review", "Continue"), fill("p6", f"{base}/apply/payment", "Pay at the counter"),
            finish("p7", "filed the permit application"), ([], "Filed."),
        ],
    })
    v1_to_v2 = {pm.LABELS["v1"][k][1]: pm.LABELS["v2"][k][1] for k in pm.LABELS["v1"]}
    v1_to_v2.update({pm.BUTTONS["v1"][k][1]: pm.BUTTONS["v2"][k][1] for k in pm.BUTTONS["v1"]})

    def repair(kw):   # a model that knows "Full name" and "Applicant's name" are the same thing
        q = json.loads(kw["messages"][1]["content"])
        listed = json.loads(next(v for k, v in q.items() if k.startswith("page_"))["content"])
        want = v1_to_v2.get(q["used_to_be"], "").lower()
        return {"target": next((c["label"] for c in listed if c["label"].lower() == want), "none")}
    chat = Chat(routine_contract=contract_answer, routine_code=code_answer, routine_repair=repair)
    policy = Policy.load(cfg.zones_file, cfg.roots)
    settings = FormSettings(profile_dir=tmp_path / "browser-profile", media_dir=tmp_path / "media",
                            vault_path=tmp_path / "profile.json", confirm_wait_ms=800)
    Vault(settings.vault_path).save({"profile": PROFILE, "answers": {}, "forms": {}})
    forms = FormService(settings, None, policy, None,
                        driver_factory=lambda: BrowserDriver(settings.profile_dir, executable_path=chromium))
    core = Core(cfg, Both(responses, chat), policy=policy, forms=forms, reminder_tick=0, workers=3)
    forms.memory = core.memory
    core.compiler.sandbox = lambda b: start(app.default_layout)[0]   # a fresh portal, same layout, to verify on
    approvals = []
    events = []
    core.subscribe(events.append)

    def tap(kind, a):
        if kind == "requested":
            approvals.append(a.action)
            core.bus.decide(a.id, a.nonce, True, by="test")
    core.bus.subscribe(tap)

    # 1. The agent does it the first time (model calls), and Lantern offers to learn it.
    first = core.submit(goal)
    assert core.wait_idle(120)
    assert core.memory.get_task(first).status == "done"
    assert {"type": "learn_offer", "task_id": first, "auto": False}.items() <= \
        next(e for e in events if e["type"] == "learn_offer").items()
    first_calls = core.memory.task_usage(first)["calls"]
    assert first_calls >= 12

    # 2. Learn: Spark names it and writes the code; it is verified on a fresh copy of the portal.
    job = core.learn(first)
    assert core.wait_idle(120)
    assert core.memory.get_task(job).status == "done", core.memory.get_task(job).summary
    [row] = core.routines()
    assert row["name"] == "city-permit-application" and row["trusted"] and row["version"] == 1
    r1 = core.compiler.store.load("city-permit-application")
    assert check(r1.code) == [] and "browser.submit('Submit application')" in r1.code
    assert {"name": "work_description", "type": "string"}.items() <= next(
        p for p in r1.contract["params"] if p["name"] == "work_description").items()
    assert "{{vault.full_name}}" in json.dumps(r1.contract) and "Asha Rao" not in json.dumps(r1.contract)
    assert len(chat.of("routine_contract")) == 1 and len(chat.of("routine_code")) == 1
    assert core.memory.task_usage(job)["calls"] == 2                     # learning cost: two Spark calls

    # 3. The same goal again: the routine runs at rung 0, one approval, zero model calls.
    approvals.clear()
    second = core.submit("[fake] " + goal)
    assert core.wait_idle(120)
    t2 = core.memory.get_task(second)
    assert t2.status == "done" and "0 model calls" in t2.summary, t2.summary
    assert core.memory.task_usage(second)["calls"] == 0 and approvals == ["run_routine"]
    assert len(json.loads(pm.json.dumps(list(app.applications)))) == 2   # the live portal got a second application
    assert "Learned routine city-permit-application v1" in as_text(build(core.memory, core.audit, second))

    # 4. The portal is redesigned (v2: every label, id and button changes). The routine heals, step by step.
    app.default_layout = "v2"
    third = core.submit(goal)
    assert core.wait_idle(180)
    t3 = core.memory.get_task(third)
    assert t3.status == "done" and "healed, saved as v2" in t3.summary, t3.summary
    repairs = chat.of("routine_repair")
    assert len(repairs) == core.memory.task_usage(third)["calls"] and repairs
    assert all(r["reasoning_effort"] == "low" and r["model"] == "muse-spark-1.3" for r in repairs)
    assert len(app.applications) == 3
    healed = core.compiler.store.load("city-permit-application")
    assert healed.version == 2 and healed.trusted and "Applicant's name" in healed.code
    assert any(e["type"] == "routine_healed" for e in events)

    # 5. Run again on v2: back to zero model calls, no repair.
    fourth = core.submit(goal)
    assert core.wait_idle(120)
    t4 = core.memory.get_task(fourth)
    assert t4.status == "done" and core.memory.task_usage(fourth)["calls"] == 0, t4.summary
    assert len(chat.of("routine_repair")) == len(repairs) and len(app.applications) == 4
    summary = core.compiler.store.summary("city-permit-application")
    assert (summary["runs"], summary["heals"], summary["version"]) == (3, 1, 2)
    assert summary["saved_usd"] >= 0 and summary["learned_from"] == first
    core.shutdown()


# ---- in the scheduler, with a stand-in browser ----------------------------------------------------------------
class StubForms:
    """Stands in for FormService: fill_form 'fills' the mini form and leaves a trace, like the real one."""

    def __init__(self, tmp_path, page, memory_ref):
        self.settings = FormSettings(profile_dir=tmp_path / "bp", media_dir=tmp_path / "media",
                                     vault_path=tmp_path / "profile.json")
        Vault(self.settings.vault_path).save({"profile": {"full name": "Asha Rao"}, "answers": {}, "forms": {}})
        self.page, self.memory_ref, self.client = page, memory_ref, None
        self.driver_factory = lambda: Driver(page)

    @property
    def vault(self):
        return Vault(self.settings.vault_path)

    def fill(self, url, goal, *, task_id=None, **kw):
        self.memory_ref[0].log_step(task_id, "trace", {
            "tool": "fill_form", "url": url, "submit": "Next", "submitted": True, "final_url": url.replace("/a", "/b"),
            "verified": True, "advanced": False, "evidence": "All done. Reference AB-123", "visual": [],
            "left_for_you": [], "fields": [{"label": "Name", "name": "n_v1", "html_id": "", "type": "text",
                                            "value": "Asha Rao"}]})
        return {"done": True, "verified": True, "submitted": True, "url": url}


def mini_core(tmp_path, responses, chat, layout="v1"):
    page = Page(layout)
    ref = [None]
    forms = StubForms(tmp_path, page, ref)
    core = Core(config(tmp_path), Both(responses, chat), forms=forms, reminder_tick=0, workers=2)
    ref[0] = core.memory
    approve_all = lambda kind, a: core.bus.decide(a.id, a.nonce, True, by="test") if kind == "requested" else None
    core.bus.subscribe(approve_all)
    return core, page


def agent_script(n):
    return [([call("fill_form", f"m{i}", url="http://127.0.0.1:9/a", goal="the mini form")], "")
            if j == 0 else (finish(f"f{i}", "sent the mini form") if j == 1 else ([], "Sent."))
            for i in range(n) for j in range(3)]


def test_a_repeated_goal_is_learned_by_itself_and_then_runs_with_no_model(tmp_path):
    from lantern.channels import ChannelHub, FakeChannel
    chat = Chat(routine_contract=lambda kw: {"name": "mini-form", "title": "Mini form", "examples": [],
                                             "steps": [], "new_params": []},
                routine_code=code_answer)
    core, page = mini_core(tmp_path, Routed({"mini form": agent_script(2)}), chat)
    phone = FakeChannel()
    hub = ChannelHub(core, [phone]).start(listen=False)
    hub.receive(phone.incoming("send the mini form"))
    hub.wait_idle()
    assert core.wait_idle(10)
    until(lambda: any("Want me to learn this?" in m["text"] for m in phone.messages()))
    hub.receive(phone.incoming("send the mini form"))                     # the same goal again: learned by itself
    hub.wait_idle()
    assert core.wait_idle(10)
    until(lambda: core.routines() and core.routines()[0]["trusted"])
    assert core.wait_idle(10)
    hub.wait_idle()
    texts = [m["text"] for m in phone.messages()]
    assert any("You've asked for this before" in t for t in texts)
    assert any(t.startswith("Learned 'mini-form' v1") for t in texts), texts
    verify = json.loads((tmp_path / "routines" / "mini-form" / "v1" / "verify.json").read_text(encoding="utf-8"))
    assert verify["mode"] == "dry" and verify["ok"] and verify["stopped_at"] == "s3"   # no sandbox: dry, nothing sent
    third = core.submit("send the mini form")
    assert core.wait_idle(10)
    assert core.memory.get_task(third).status == "done" and core.memory.task_usage(third)["calls"] == 0
    assert page.clicks == ['[data-lantern-btn="0"]']                     # the one real submit, by the routine
    hub.stop()
    core.shutdown()


def seed_mini(core, layout_ok=True):
    c = dict(MINI, source={"task_id": 1, "cost_usd": 0.02})
    return core.compiler.store.save(c, render(c), {"mode": "dry", "ok": True}, how="test", trusted=True)


def test_a_routine_that_cant_heal_falls_back_to_the_agent_when_nothing_was_sent(tmp_path):
    chat = Chat(routine_repair=lambda kw: {"target": "Banana"})           # not on the page: rejected, a rung up
    core, page = mini_core(tmp_path, Routed({"mini form": agent_script(1)}), chat, layout="v2")
    seed_mini(core)
    events = []
    core.subscribe(events.append)
    tid = core.submit("send the mini form")
    assert core.wait_idle(10)
    t = core.memory.get_task(tid)
    assert t.status == "done" and t.summary == "sent the mini form"       # the agent did it instead
    assert any(e["type"] == "routine_fallback" and e["step"] == "s2" for e in events)
    assert page.clicks == [] and len(chat.of("routine_repair")) == 2      # tried twice, the second a rung up
    assert [r["reasoning_effort"] for r in chat.of("routine_repair")] == ["low", "high"]
    core.shutdown()


def test_a_routine_cut_off_by_a_restart_after_it_submitted_is_never_run_again(tmp_path):
    core, page = mini_core(tmp_path, Routed({}), Chat())
    seed_mini(core)
    tid = core.memory.start_task("send the mini form").id
    core.memory.save_task_state(tid, "running", "", {"goal": "send the mini form", "routine": "mini-form",
                                                     "version": 1, "acted": True})
    core.shutdown()
    core2, page2 = mini_core(tmp_path, Routed({}), Chat())
    t = core2.memory.get_task(tid)
    assert t.status == "failed" and "nothing was repeated" in t.summary and page2.clicks == []
    assert core2.memory.task_states() == []
    core2.shutdown()


def test_model_code_that_breaks_the_rules_is_replaced_by_lanterns_own(tmp_path):
    chat = Chat(routine_contract=lambda kw: {"name": "mini-form", "title": "Mini", "examples": [], "steps": [],
                                             "new_params": []},
                routine_code=lambda kw: {"code": "import os\ndef run(params):\n    os.system('curl evil')\n"})
    core, page = mini_core(tmp_path, Routed({"mini form": agent_script(1)}), chat)
    tid = core.submit("send the mini form")
    assert core.wait_idle(10)
    job = core.learn(tid)
    assert core.wait_idle(10)
    assert core.memory.get_task(job).status == "done"
    verify = json.loads((tmp_path / "routines" / "mini-form" / "v1" / "verify.json").read_text(encoding="utf-8"))
    assert verify["code_by"].startswith("rules (the model's code was rejected: line 1: the only import allowed")
    assert len(chat.of("routine_code")) == 2                               # one retry with the reasons, then Lantern's
    assert "os.system" not in core.compiler.store.load("mini-form").code
    core.shutdown()


def test_matching_by_words_and_enum_values_and_by_one_cheap_call(tmp_path):
    chat = Chat(routine_match=lambda kw: {"routine": "city-permit", "params": [
        {"name": "permit_type", "value": "street_event"}, {"name": "base_url", "value": "http://evil.example"}]})
    core, page = mini_core(tmp_path, Routed({}), chat)
    c = skeleton_from_traces("fill the city permit application", TRACES, {"full name": "Asha Rao"}, {})
    c["name"] = "city-permit"
    core.compiler.store.save(c, render(c), {"ok": True}, how="test", trusted=True)
    m = core.compiler.match("[whatsapp] fill the city permit application")
    assert m.how == "words" and m.params == {}
    m = core.compiler.match("please fill the city permit application for a street event")
    assert m.how == "words" and m.params == {"permit_type": "street_event"}
    assert core.compiler.match("fill the tax form") is None
    assert core.compiler.match("[step by step] fill the city permit application") is None
    client = Both(None, chat)
    m = core.compiler.match("submit my permit paperwork to the city", client=client)
    assert m.how == "model" and m.params == {"permit_type": "street_event"}   # base_url isn't the model's to set...
    assert m.routine.name == "city-permit"
    [req] = chat.of("routine_match")
    assert req["reasoning_effort"] == "low" and "Asha" not in json.dumps(req)   # ...and no private value is sent
    core.shutdown()


def test_what_cant_be_learned_says_why(tmp_path):
    core, page = mini_core(tmp_path, Routed({}), Chat())
    t = core.memory.start_task("list my folders")
    core.memory.finish_task(t.id, "done", "listed")
    with pytest.raises(ValueError, match="didn't fill any form"):
        core.learn(t.id)
    u = core.memory.start_task("fill a form")
    core.memory.log_step(u.id, "trace", dict(TRACES[0], visual=["Category"]))
    core.memory.finish_task(u.id, "done", "filled")
    with pytest.raises(ValueError, match="needed computer use"):
        core.learn(u.id)
    core.shutdown()


def test_routines_on_the_api(tmp_path):
    from fastapi.testclient import TestClient
    from lantern.server import create_app
    core, page = mini_core(tmp_path, Routed({}), Chat())
    seed_mini(core)
    api = TestClient(create_app(core, "tok"))
    h = {"X-Lantern-Token": "tok"}
    [row] = api.get("/routines", headers=h).json()
    assert row["name"] == "mini-form" and row["trusted"] and row["version"] == 1
    t = core.memory.start_task("x")
    core.memory.finish_task(t.id, "done", "x")
    assert api.post("/routines/learn", json={"task_id": t.id}, headers=h).status_code == 409
    assert api.post("/routines/learn", json={"task_id": 999}, headers=h).status_code == 404
    assert api.post("/routines/mini-form/forget", headers=h).json() == {"forgot": True}
    assert api.get("/routines", headers=h).json() == []
    core.shutdown()
