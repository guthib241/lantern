"""Live, Oct 27-28: the Responses API dropped a task's chain mid-task with 400 "referenced response not found or
expired". The agent continues on a fresh chain with a recap instead of failing the task. Scripted models only."""
from types import SimpleNamespace as NS

from fakes import FakeClient, call
from lantern.agent import Agent, chain_lost
from lantern.memory import Memory
from lantern.tools import Toolbox


class Lost(Exception):
    def __init__(self, text, status=400):
        super().__init__(text)
        self.status_code, self.body = status, {"error": {"message": text}}
        self.response = NS(status_code=status, headers={})


def test_the_live_wording_counts_as_a_lost_chain():
    assert chain_lost(Lost("referenced response not found or expired"))
    assert chain_lost(Lost("previous_response_id resp_1 not found"))
    assert not chain_lost(Lost("tool_choice must be auto"))
    assert not chain_lost(Lost("referenced response not found or expired", status=500))


class DropsOnce(FakeClient):
    def create(self, **kw):
        if kw.get("previous_response_id") and not getattr(self, "dropped", False):
            self.dropped = True
            self.requests.append(kw)
            raise Lost("referenced response not found or expired")
        return super().create(**kw)


def test_the_agent_continues_with_a_recap(tmp_path):
    (tmp_path / "docs").mkdir()
    mem = Memory(str(tmp_path / "m.db"))
    tools = Toolbox([tmp_path / "docs"], mem, lambda d: True)
    client = DropsOnce([([call("list_folders", "c1")], ""),
                        ([call("finish", "c2", status="done", summary="listed")], ""), ([], "Done.")])
    r = Agent(client, mem, tools).run("list my folders")
    assert r.status == "done" and r.summary == "listed"
    retry = client.requests[2]
    assert "previous_response_id" not in retry and "list_folders" in retry["input"]
    assert any("no longer had" in str(s["content"]) for s in mem.steps(r.task_id))


def test_a_recap_keeps_verified_and_lists_what_must_never_be_redone(tmp_path):
    """Live, Oct 28: raw results cut at 300 characters hid `verified: true`; the model filed the portal twice."""
    from lantern.agent import _recap_step
    big = {"url": "http://p/apply/payment", "page": "Fee", "filled": [{"field": "x" * 50, "value": "y"}] * 20,
           "submitted": True, "verified": True}
    step = _recap_step({"name": "fill_form", "args": {"url": "http://p/apply/payment"}, "result": big})
    assert '"verified": true' in step and '"submitted": true' in step and "xxxx" not in step
    assert _recap_step({"name": "ask_user", "args": {}, "result": {"answer": "teal"}}).endswith('"teal"}')

    (tmp_path / "docs").mkdir()
    mem = Memory(str(tmp_path / "m.db"))
    tools = Toolbox([tmp_path / "docs"], mem, lambda d: True)
    agent = Agent(DropsOnce([([], "ok")]), mem, tools)
    t = mem.start_task("file the permit")
    agent.client.dropped = False
    calls = [{"name": "fill_form", "args": {"url": "http://p/apply/payment"}, "result": big}]
    agent._follow(t.id, "file the permit", "rules", "resp_0", [], calls)
    recap = agent.client.requests[-1]["input"]
    assert "ALREADY DONE AND CONFIRMED, never do these again: http://p/apply/payment" in recap


def test_a_form_page_confirmed_once_is_never_submitted_again_in_the_task(tmp_path):
    (tmp_path / "docs").mkdir()
    mem = Memory(str(tmp_path / "m.db"))
    calls = []

    class Forms:
        def fill(self, url, goal, **kw):
            calls.append(url)
            return {"url": url, "submitted": True, "verified": True, "done": True}
    tools = Toolbox([tmp_path / "docs"], mem, lambda d: True, forms=Forms())
    tools.begin_task(1)
    assert tools.fill_form("http://p/apply/payment", "pay at the counter")["verified"] is True
    again = tools.fill_form("http://p/apply/payment?x=1", "pay at the counter")
    assert again["already_filed"] is True and again["submitted"] is False and calls == ["http://p/apply/payment"]
    tools.begin_task(2)                                  # a new task may file a new application
    tools.fill_form("http://p/apply/payment", "pay at the counter")
    assert len(calls) == 2


def test_the_computer_use_loop_survives_a_lost_chain():
    from lantern.forms.computer import FUNCTION_TOOLS, ComputerLoop

    class Client:
        def __init__(self):
            self.requests, self.responses = [], self

        def create(self, **kw):
            self.requests.append(kw)
            if kw.get("previous_response_id") and len(self.requests) == 2:
                raise Lost("referenced response not found or expired")
            if len(self.requests) == 1:
                return NS(id="r1", output=[NS(type="function_call", name="computer.computer", call_id="c1",
                                              arguments='{"type": "wait"}')], output_text="", usage=None)
            return NS(id="r3", output=[NS(type="function_call", name="computer.stop", call_id="c2",
                                          arguments='{"answer": "done"}')], output_text="", usage=None)

    class Driver:
        width, height, url = 1280, 800, "http://127.0.0.1/form"

        def screenshot(self):
            return b"\xff\xd8jpeg"

        def blocked_submits(self):
            return 0

        def __getattr__(self, name):
            return lambda *a, **k: None
    client = Client()
    loop = ComputerLoop(client, Driver(), model="muse-wonder-1.0", protocol="function",
                        approve_safety=lambda c, a: True)
    r = loop.run("Fill the name field")
    assert r.status == "done" and len(client.requests) == 3
    fresh = client.requests[2]
    assert "previous_response_id" not in fresh and fresh["tools"] == FUNCTION_TOOLS
    assert "Continuing" in fresh["input"][0]["content"][0]["text"]
