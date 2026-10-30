"""A form submitted and verified in a task is never submitted again in that task (live, Oct 28, claude/real-run:
after a lost response chain the model started the portal again and filed it twice)."""
import json

from lantern.memory import Memory
from lantern.tools import Toolbox, form_page


class Forms:
    def __init__(self):
        self.calls = []

    def fill(self, url, goal, **kw):
        self.calls.append(url)
        return {"done": True, "submitted": True, "verified": True, "confirmation": {"content": "Ref AB-12"}}


def toolbox(tmp_path, forms, memory=None):
    tb = Toolbox([tmp_path], memory or Memory(tmp_path / "m.db"), lambda d: True, forms=forms)
    return tb


def test_the_same_form_is_never_filed_twice_in_one_task(tmp_path):
    forms = Forms()
    tb = toolbox(tmp_path, forms)
    tb.begin_task(1, "apply for the permit")
    first = tb.fill_form("http://127.0.0.1:8770/apply?step=1", "apply")
    assert first["submitted"] and first["form_url"] == "http://127.0.0.1:8770/apply"
    again = tb.fill_form("http://127.0.0.1:8770/apply/#top", "apply again")
    assert again["already_filed"] and not again["submitted"] and "Finish the task" in again["reason"]
    assert forms.calls == ["http://127.0.0.1:8770/apply?step=1"]
    tb.begin_task(2, "apply for a second permit")   # another task may file it
    assert tb.fill_form("http://127.0.0.1:8770/apply", "apply")["submitted"]
    assert len(forms.calls) == 2


def test_the_guard_holds_after_a_restart_from_the_step_log(tmp_path):
    mem = Memory(tmp_path / "m.db")
    tid = mem.start_task("apply for the permit").id
    mem.log_step(tid, "tool_result", {"name": "fill_form", "result": {
        "submitted": True, "verified": True, "form_url": form_page("http://127.0.0.1:8770/apply")}})
    forms = Forms()
    tb = toolbox(tmp_path, forms, mem)   # a new process: nothing in memory but the log
    tb.begin_task(tid, "apply for the permit")
    out = tb.fill_form("http://127.0.0.1:8770/apply", "apply")
    assert out["already_filed"] and forms.calls == []
    assert json.loads(json.dumps(out))["form_url"] == "http://127.0.0.1:8770/apply"


def test_a_form_that_was_not_confirmed_can_be_tried_again(tmp_path):
    class Unconfirmed(Forms):
        def fill(self, url, goal, **kw):
            self.calls.append(url)
            return {"done": False, "submitted": True, "verified": False}
    forms = Unconfirmed()
    tb = toolbox(tmp_path, forms)
    tb.begin_task(1, "apply")
    tb.fill_form("http://x.example/apply", "apply")
    tb.fill_form("http://x.example/apply", "apply")
    assert len(forms.calls) == 2
