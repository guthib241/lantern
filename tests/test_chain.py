"""The API can drop a task's response chain mid-task. Seen live on Oct 28 (scripts/phase2_live.py check 6):
400 invalid_request_error "referenced response not found or expired". Lantern continues on a fresh chain with
a recap instead of failing the task. The model is scripted; the error is the live one, word for word."""
from fakes import call
from lantern.core import Core
from lantern.taskrun import chain_lost
from test_scheduler import Routed, config, finish

LIVE = "Error code: 400 - {'error': {'code': None, 'message': 'referenced response not found or expired', " \
       "'param': None, 'type': 'invalid_request_error'}}"


class BadRequest(Exception):
    status_code = 400


class DropsTheChain:
    """Passes calls to the scripted model, but fails the first chained call the way the live API did."""

    def __init__(self, routed):
        self.routed, self.responses, self.requests, self.dropped = routed, self, [], 0

    def create(self, **kw):
        self.requests.append(kw)
        if kw.get("previous_response_id") and not self.dropped:
            self.dropped += 1
            raise BadRequest(LIVE)
        return self.routed.create(**kw)


def test_the_live_error_is_recognised_and_nothing_else_is():
    assert chain_lost(BadRequest(LIVE))
    assert chain_lost(BadRequest("previous_response_id not found"))
    assert not chain_lost(BadRequest("Error code: 400 - invalid tool schema"))
    assert not chain_lost(type("ServerError", (Exception,), {"status_code": 500})("referenced response expired"))


def test_a_dropped_chain_mid_task_continues_with_a_recap(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "notes.txt").write_text("hello")
    client = DropsTheChain(Routed({"notes": [
        ([call("list_dir", "c1", path=str(root))], ""),
        finish("c2", "listed the notes"),
        ([], "Your docs folder has notes.txt."),
    ]}))
    core = Core(config(tmp_path), client, reminder_tick=0, workers=1)
    tid = core.submit("list my notes")
    assert core.wait_idle(20)
    t = core.memory.get_task(tid)
    assert t.status == "done", t.summary
    assert client.dropped == 1
    notes = [s["content"] for s in core.memory.steps(tid) if s["kind"] == "note"]
    assert any("no longer had this task's earlier turns" in n for n in notes)
    recap = next(r for r in client.requests[1:] if not r.get("previous_response_id"))
    assert "notes" in str(recap["input"])                  # the fresh chain carries the goal and what was done
    core.shutdown()
