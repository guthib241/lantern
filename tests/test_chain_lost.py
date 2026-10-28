"""Live, Oct 27-28: the Responses API dropped a task's chain mid-task, seconds after the response was made, with
400 "referenced response not found or expired". Both the scheduler's agent and the one-at-a-time agent must
continue on a fresh chain with a recap instead of failing the task. All models are scripted."""
from types import SimpleNamespace as NS

from fakes import FakeClient, call
from lantern.agent import Agent
from lantern.memory import Memory
from lantern.policy import WRITABLE, Policy, Rule
from lantern.taskrun import chain_lost
from lantern.tools import Toolbox


class Lost(Exception):
    """Shaped like the OpenAI SDK's BadRequestError."""

    def __init__(self, text):
        super().__init__(text)
        self.status_code, self.body = 400, {"error": {"message": text}}
        self.response = NS(status_code=400, headers={})


def test_the_live_wording_counts_as_a_lost_chain():
    assert chain_lost(Lost("referenced response not found or expired"))
    assert chain_lost(Lost("previous_response_id resp_1 not found"))
    assert not chain_lost(Lost("tool_choice must be auto"))
    e = Lost("referenced response not found or expired")
    e.status_code = e.response.status_code = 500
    assert not chain_lost(e)


class DropsOnce(FakeClient):
    def create(self, **kw):
        if kw.get("previous_response_id") and not getattr(self, "dropped", False):
            self.dropped = True
            self.requests.append(kw)
            raise Lost("referenced response not found or expired")
        return super().create(**kw)


def test_the_one_at_a_time_agent_continues_with_a_recap(tmp_path):
    (tmp_path / "docs").mkdir()
    mem = Memory(str(tmp_path / "m.db"))
    tools = Toolbox(Policy([Rule.make(tmp_path / "docs", WRITABLE)]), mem, lambda d: True)
    client = DropsOnce([([call("list_folders", "c1")], ""),
                        ([call("finish", "c2", status="done", summary="listed")], ""), ([], "Done.")])
    r = Agent(client, mem, tools).run("list my folders")
    assert r.status == "done" and r.summary == "listed"
    retry = client.requests[2]                  # after the 400: a fresh chain whose input recaps the steps so far
    assert "previous_response_id" not in retry and "list_folders" in retry["input"]
    assert any("no longer had" in str(s["content"]) for s in mem.steps(r.task_id))
