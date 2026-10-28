"""S2: the resilience engine. Scripted failures of each class; the right strategy for each; a send, submit or
move is never repeated. All model calls are scripted (FakeClient per task); nothing reaches the real API."""
import json
import threading

from fakes import FakeClient, call
from lantern.budget import BudgetError
from lantern.core import Core
from lantern.memory import Memory
from lantern.receipt import as_text, build
from lantern.recovery import Limits, Recovery, effect_key
from test_scheduler import Routed, approve_all, ask, config, finish, until

FAST = Limits(transient_wait=0.01)


def attempts(core, tid):
    return [json.loads(s["content"]) for s in core.memory.steps(tid) if s["kind"] == "attempt"]


def outputs(fake, i):
    return [json.loads(o["output"]) for o in fake.requests[i]["input"]]


def core_for(tmp_path, client, forms=None, limits=FAST, workers=2):
    core = Core(config(tmp_path), client, reminder_tick=0, workers=workers, forms=forms)
    core.scheduler.retry = {"base_delay": 0.01, "max_delay": 0.02, "max_retries": 1}
    core.scheduler.recovery_limits = limits
    return core


class ScriptedForms:
    """Stands in for FormService: each fill returns the next scripted result (no browser)."""

    def __init__(self, results):
        self.results = list(results)
        self.client = None
        self.fills = []

    def fill(self, url, goal, **kw):
        self.fills.append((url, goal))
        return dict(self.results.pop(0), url=url)


# ---- transient ----------------------------------------------------------------------------
def test_transient_errors_back_off_parked_then_succeed(tmp_path):
    class Busy(Exception):
        status_code = 503
    fails = {"n": 0}
    client = Routed({"report": [finish("f", "made the report"), ([], "ok")]})
    real = client.create

    def create(**kw):
        if fails["n"] < 3:   # the client's own retry (1) runs out, then recovery waits and tries again
            fails["n"] += 1
            raise Busy("upstream busy")
        return real(**kw)
    client.create = create
    core = core_for(tmp_path, client)
    events = []
    core.subscribe(events.append)
    tid = core.submit("make the report")
    assert core.wait_idle(5)
    assert core.memory.get_task(tid).status == "done"
    [a] = attempts(core, tid)
    assert (a["class"], a["strategy"]) == ("transient", "backoff")
    assert any(e["type"] == "task_parked" and e["reason"] == "backoff" for e in events)
    core.shutdown()


def test_a_spending_limit_is_impossible_with_a_report(tmp_path):
    client = Routed({"thing": [finish("f"), ([], "ok")]})
    core = core_for(tmp_path, client)
    real = client.create

    def create(**kw):
        raise BudgetError("This task reached its $3.00 limit")
    client.create = create
    tid = core.submit("do the thing")
    assert core.wait_idle(5)
    t = core.memory.get_task(tid)
    assert t.status == "failed" and t.summary.startswith("Stopped: This task reached its $3.00 limit")
    assert attempts(core, tid)[-1]["class"] == "impossible"
    client.create = real
    core.shutdown()


# ---- tool_error ----------------------------------------------------------------------------
def test_a_tool_error_is_fed_back_and_the_same_failing_call_is_refused(tmp_path):
    missing = str(tmp_path / "docs" / "nope.txt")
    client = Routed({"notes": [([call("read_file", "r1", path=missing)], ""),
                               ([call("read_file", "r2", path=missing)], ""),
                               ([call("list_folders", "l1")], ""), finish("f"), ([], "ok")]})
    core = core_for(tmp_path, client)
    tid = core.submit("read my notes")
    assert core.wait_idle(5)
    fake = client.fakes["notes"]
    first, second = outputs(fake, 1)[0], outputs(fake, 2)[0]
    assert first["recovery"]["class"] == "tool_error" and first["recovery"]["strategy"] == "feed_back"
    assert second["error"].startswith("refused: the same call just failed")
    assert [(a["class"], a["strategy"]) for a in attempts(core, tid)] == [("tool_error", "feed_back"),
                                                                         ("tool_error", "different_approach")]
    assert core.memory.get_task(tid).status == "done"
    core.shutdown()


# ---- approach_failed ----------------------------------------------------------------------------
def test_an_unverified_step_gets_the_next_approach(tmp_path):
    forms = ScriptedForms([{"done": False, "verified": False, "submitted": False, "reason": "the date was rejected"},
                           {"done": True, "verified": True, "submitted": True}])
    url = "http://127.0.0.1:9/apply"
    client = Routed({"permit": [([call("fill_form", "a", url=url, goal="permit")], ""),
                                ([call("fill_form", "b", url=url, goal="permit, date as 2026-11-20")], ""),
                                finish("f"), ([], "ok")]})
    core = core_for(tmp_path, client, forms=forms)
    approve_all(core)
    tid = core.submit("fill the permit form")
    assert core.wait_idle(5)
    first = outputs(client.fakes["permit"], 1)[0]
    assert first["recovery"]["class"] == "approach_failed" and first["recovery"]["strategy"] == "next_approach"
    assert "stronger model" in first["recovery"]["next"]
    assert len(forms.fills) == 2          # nothing was submitted the first time, so trying again is allowed
    assert core.memory.get_task(tid).status == "done"
    core.shutdown()


# ---- model_confused ----------------------------------------------------------------------------
def test_unreadable_arguments_twice_and_loops_escalate(tmp_path):
    bad = lambda cid: [type("FC", (), {"type": "function_call", "name": "list_dir", "call_id": cid, "arguments": "{oops"})()]
    client = Routed({"folders": [(bad("x1"), ""), (bad("x2"), ""),
                                 ([call("list_folders", "l1")], ""), ([call("list_folders", "l2")], ""),
                                 ([call("list_folders", "l3")], ""), finish("f"), ([], "ok")]})
    core = core_for(tmp_path, client)
    escalations = []
    core.subscribe(lambda e: escalations.append(e) if e["type"] == "recovery" and e["strategy"] == "escalate" else None)
    core.submit("list my folders")
    assert core.wait_idle(5)
    assert [e["result"] for e in escalations] == ["unreadable tool arguments twice", "the same call three times"]
    assert [e["to_rung"] for e in escalations] == [4, 5]                 # Spark high, then Wonder (S3)
    reqs = client.fakes["folders"].requests
    assert (reqs[0]["model"], reqs[0]["reasoning"]) == ("muse-spark-1.3", {"effort": "low"})
    assert (reqs[2]["model"], reqs[2]["reasoning"]) == ("muse-spark-1.3", {"effort": "high"})
    assert reqs[5]["model"] == "muse-wonder-1.0" and "previous_response_id" not in reqs[5]
    assert "Continuing task" in reqs[5]["input"] and "refused: this would be the 3rd identical call" in reqs[5]["input"]
    core.shutdown()


def test_no_progress_for_n_turns_escalates(tmp_path):
    client = Routed({"spin": [([call("read_file", f"r{i}", path=str(tmp_path / f"none{i}.txt"))], "") for i in range(3)]
                     + [finish("f"), ([], "ok")]})
    core = core_for(tmp_path, client, limits=Limits(transient_wait=0.01, no_progress_turns=3))
    tid = core.submit("spin around")
    assert core.wait_idle(5)
    assert ("model_confused", "escalate") in [(a["class"], a["strategy"]) for a in attempts(core, tid)]
    core.shutdown()


# ---- needs_human ----------------------------------------------------------------------------
def test_a_step_only_the_user_can_do_goes_to_ask_user_and_parks(tmp_path):
    forms = ScriptedForms([{"done": False, "verified": False, "submitted": False, "needs_user": True,
                            "reason": "There's a CAPTCHA on this form: you do this part. Nothing was submitted."}])
    client = Routed({"visa": [([call("fill_form", "a", url="http://127.0.0.1:9/visa", goal="visa")], ""),
                              ask("q", "The visa form has a CAPTCHA. Solve it in the browser, then reply done."),
                              finish("f", "user finished the CAPTCHA part"), ([], "ok")]})
    core = core_for(tmp_path, client, forms=forms)
    tid = core.submit("fill the visa form")
    until(lambda: core.questions())
    hint = outputs(client.fakes["visa"], 1)[0]["recovery"]
    assert hint["class"] == "needs_human" and hint["strategy"] == "ask_user" and "ask_user" in hint["next"]
    core.answer(tid, "done")
    assert core.wait_idle(5)
    assert core.memory.get_task(tid).status == "done"
    core.shutdown()


# ---- "done" only when the last change verified ---------------------------------------------
UNPAID = {"done": False, "verified": False, "submitted": True, "url": "http://127.0.0.1:9/apply/payment",
          "reason": "no confirmation appeared after Submit; check the screenshot"}
PAID = {"done": True, "verified": True, "submitted": True, "url": "http://127.0.0.1:9/apply/payment"}


def test_done_after_an_unverified_change_is_refused_once_then_recorded_as_failed(tmp_path):
    """Seen live (eval_privacy, masked run 5): the declaration box stayed unticked, the payment page said so, and the
    agent still finished "done", saying the declaration was accepted."""
    forms = ScriptedForms([UNPAID])
    client = Routed({"permit": [([call("fill_form", "a", url=UNPAID["url"], goal="pay")], ""),
                                finish("f1", "Completed with counter payment and declaration"),
                                finish("f2", "Completed with counter payment and declaration"), ([], "ok")]})
    core = core_for(tmp_path, client, forms=forms)
    tid = core.submit("fill the permit payment page")
    assert core.wait_idle(5)
    refused = outputs(client.fakes["permit"], 2)[0]
    assert "wasn't confirmed (no confirmation appeared after Submit" in refused["error"]
    t = core.memory.get_task(tid)
    assert t.status == "failed" and t.summary.startswith("Not confirmed: no confirmation appeared")
    core.shutdown()


def test_only_the_same_form_verifying_or_the_users_word_clears_it_and_both_paths_agree(tmp_path):
    other = "http://127.0.0.1:8/newsletter"   # another site: its success says nothing about the payment
    forms = ScriptedForms([UNPAID, {**PAID, "url": other}])
    client = Routed({"permit": [([call("fill_form", "a", url=UNPAID["url"], goal="pay")], ""),
                                ([call("fill_form", "b", url=other, goal="review")], ""),   # a success elsewhere
                                finish("f1", "done"),
                                ask("q", "The payment page wants the declaration ticked: tick it, then reply done."),
                                finish("f2", "paid at the counter; you ticked the declaration"), ([], "ok")]})
    core = core_for(tmp_path, client, forms=forms)
    tid = core.submit("fill the permit payment page")
    until(lambda: core.questions())                                               # refused: still open
    assert "wasn't confirmed" in outputs(client.fakes["permit"], 3)[0]["error"]
    core.answer(tid, "done")
    assert core.wait_idle(5) and core.memory.get_task(tid).status == "done"     # the user's word confirms
    core.shutdown()
    from fakes import FakeClient
    one = FakeClient([([call("fill_form", "a", url=UNPAID["url"], goal="pay")], ""), finish("f1", "done"),
                      finish("f2", "done"), ([], "ok")])                         # one at a time (no scheduler)
    (tmp_path / "solo").mkdir()
    solo = Core(config(tmp_path / "solo"), one, reminder_tick=0, forms=ScriptedForms([UNPAID]))
    tid = solo.submit("fill the permit payment page")
    assert solo.wait_idle(5)
    assert solo.memory.get_task(tid).status == "failed" and "wasn't confirmed" in json.dumps(one.requests[2]["input"])
    solo.shutdown()


# ---- impossible ----------------------------------------------------------------------------
def test_a_step_that_keeps_failing_stops_the_task_with_a_report(tmp_path):
    forms = ScriptedForms([{"done": False, "verified": False, "submitted": False, "reason": f"rejected {i}"}
                           for i in range(3)])
    url = "http://127.0.0.1:9/apply"
    client = Routed({"form": [([call("fill_form", f"a{i}", url=url, goal=f"try {i}")], "") for i in range(3)]
                     + [finish("f"), ([], "ok")]})
    core = core_for(tmp_path, client, forms=forms)
    tid = core.submit("fill the form")
    assert core.wait_idle(5)
    t = core.memory.get_task(tid)
    assert t.status == "failed"
    assert t.summary.startswith("Stopped: fill_form failed 3 times. Tried: 1) fill_form: approach failed")
    text = as_text(build(core.memory, core.audit, tid))
    assert "↻ Attempt 3: fill_form approach failed → next approach" in text
    core.shutdown()


# ---- a send, submit or move is never repeated -----------------------------------------------
def outbox(tmp_path):
    box = tmp_path / "media" / "outbox"
    return sorted(box.glob("*.eml")) if box.exists() else []


def test_the_same_email_is_never_sent_twice(tmp_path):
    send = dict(to=["rahul@example.com"], subject="The slides", body="Here you go.")
    client = Routed({"slides": [([call("send_email", "s1", **send)], ""),
                                ([call("send_email", "s2", **{**send, "body": "Here you go!!"})], ""),
                                finish("f"), ([], "ok")]})
    core = core_for(tmp_path, client)
    approve_all(core)
    core.submit("send Rahul the slides")
    assert core.wait_idle(5)
    assert len(outbox(tmp_path)) == 1
    second = outputs(client.fakes["slides"], 2)[0]
    assert second["error"].startswith("refused: this exact send_email already ran")
    assert second["recovery"] == {"class": "needs_human", "strategy": "refuse_duplicate"}
    core.shutdown()


def test_a_send_before_a_pause_is_still_never_repeated_after_it(tmp_path):
    send = dict(to=["rahul@example.com"], subject="The slides", body="Here you go.")
    client = Routed({"slides": [([call("send_email", "s1", **send)], ""), ask("q", "Also the notes?"),
                                ([call("send_email", "s2", **send)], ""), finish("f"), ([], "ok")]})
    core = core_for(tmp_path, client)
    approve_all(core)
    tid = core.submit("send Rahul the slides")
    until(lambda: core.questions())
    core.answer(tid, "no, just resend the slides")
    assert core.wait_idle(5)
    assert len(outbox(tmp_path)) == 1
    assert outputs(client.fakes["slides"], 3)[0]["error"].startswith("refused: this exact send_email")
    core.shutdown()


def test_a_send_cut_off_by_a_restart_is_not_repeated(tmp_path):
    """Lantern died while send_email was running: the new process may not send it again."""
    send = dict(to=["rahul@example.com"], subject="The slides", body="Here you go.")
    cfg = config(tmp_path)
    mem = Memory(cfg.db_path, None)
    mem.close()
    first = Routed({"slides": [([call("send_email", "s1", **send)], ""), threading.Event()]})
    core1 = Core(cfg, first, reminder_tick=0, workers=1)
    tid = core1.submit("send Rahul the slides")
    until(lambda: core1.bus.pending())        # the send is in flight, waiting for its approval...
    state = core1.memory.load_task_state(tid)["state"]
    state["approval_open"] = False            # ...and say the yes came and the send started when Lantern died
    core1.memory.save_task_state(tid, "running", "", state)
    core1.scheduler.closing.set()

    second = Routed({"slides": [([call("send_email", "s2", **send)], ""), finish("f", "told the user"), ([], "ok")]},
                    owner={"slides-resp_1": "slides"})
    core2 = core_for(tmp_path, second)
    approve_all(core2)
    assert core2.wait_idle(5)
    told, refused = outputs(second.fakes["slides"], 0)[0], outputs(second.fakes["slides"], 1)[0]
    assert told["uncertain"] is True and told["recovery"]["class"] == "needs_human"
    assert refused["error"].startswith("refused: this exact send_email already ran in this task (or may have)")
    assert outbox(tmp_path) == []             # the new process sent nothing
    core2.shutdown()


def test_a_move_that_provably_did_not_happen_may_be_tried_again(tmp_path):
    src = tmp_path / "docs" / "a.txt"
    mem = Memory(tmp_path / "m.db")
    t = mem.start_task("tidy")
    rec = Recovery(mem, t.id, policy=type("P", (), {"resolve": staticmethod(lambda p: p)})())
    args = {"source": str(tmp_path / "docs" / "a.txt"), "destination": str(tmp_path / "docs" / "b") + "/"}
    rec.after_call(t.id, "move_file", args, {"error": "Lantern stopped while move_file was running", "uncertain": True})
    (tmp_path / "docs").mkdir(exist_ok=True)
    assert rec.before_call(t.id, "move_file", args)["error"].startswith("refused")   # gone: it may have moved
    src.write_text("still here")
    assert rec.before_call(t.id, "move_file", args) is None                         # still there: it didn't happen


def test_effect_keys_ignore_wording_but_not_targets():
    a = effect_key("send_email", {"to": ["b@x.com", "a@x.com"], "subject": "Slides", "body": "hi"})
    assert a == effect_key("send_email", {"to": ["a@x.com", "b@x.com"], "subject": "slides ", "body": "hello"})
    assert a != effect_key("send_email", {"to": ["a@x.com"], "subject": "Slides"})
    assert effect_key("fill_form", {"url": "u", "goal": "x"}) == effect_key("fill_form", {"url": "u", "goal": "y"})


def test_legacy_mode_still_runs_without_recovery(tmp_path):
    client = FakeClient([finish("f"), ([], "ok")])
    core = Core(config(tmp_path), client, reminder_tick=0)
    tid = core.submit("anything")
    assert core.wait_idle(5) and core.memory.get_task(tid).status == "done"
    core.shutdown()
