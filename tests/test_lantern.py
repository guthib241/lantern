"""Tests run fully offline: a scripted fake model replaces Muse Spark."""
import json
from types import SimpleNamespace as NS

import pytest

from lantern.agent import Agent
from lantern.memory import Memory
from lantern.tools import Toolbox, ToolError


# ---- fake Responses API ---------------------------------------------------
def call(name, cid, **args):
    return NS(type="function_call", name=name, call_id=cid, arguments=json.dumps(args))


class FakeClient:
    """Returns scripted responses in order and records every request."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.responses = self

    def create(self, **kw):
        self.requests.append(kw)
        out, text = self.script.pop(0)
        return NS(id=f"resp_{len(self.requests)}", output=out, output_text=text)


@pytest.fixture
def home(tmp_path):
    root = tmp_path / "docs"
    (root / "inbox").mkdir(parents=True)
    (root / "inbox" / "invoice_march.txt").write_text("Invoice #42 total 1200")
    (root / "notes.md").write_text("meeting with priya about the contract")
    mem = Memory(tmp_path / "mem.db")
    return root, mem


def toolbox(root, mem, answer=True):
    asked = []

    def approve(desc):
        asked.append(desc)
        return answer
    return Toolbox([root], mem, approve), asked


# ---- memory ----------------------------------------------------------------
def test_memory_tracks_tasks_steps_and_facts(home):
    _, mem = home
    t = mem.start_task("tidy downloads")
    mem.log_step(t.id, "note", {"x": 1})
    mem.finish_task(t.id, "done", "moved 3 files")
    mem.remember("Manager Email", "priya@example.com")
    assert mem.get_task(t.id).status == "done"
    assert mem.steps(t.id)[0]["kind"] == "note"
    assert mem.facts() == {"manager email": "priya@example.com"}
    ctx = mem.context_block()
    assert "priya@example.com" in ctx and "moved 3 files" in ctx


def test_memory_survives_restart(home, tmp_path):
    _, mem = home
    mem.remember("city", "Chennai")
    mem.close()
    assert Memory(tmp_path / "mem.db").facts()["city"] == "Chennai"


def test_bad_status_rejected(home):
    _, mem = home
    t = mem.start_task("x")
    with pytest.raises(ValueError):
        mem.finish_task(t.id, "great", "")


# ---- tools -----------------------------------------------------------------
def test_cannot_escape_allowed_folder(home, tmp_path):
    root, mem = home
    tb, _ = toolbox(root, mem)
    (tmp_path / "secret.txt").write_text("nope")
    with pytest.raises(ToolError):
        tb.read_file(str(root / ".." / "secret.txt"))
    with pytest.raises(ToolError):
        tb.read_file("/etc/passwd")


def test_symlink_escape_blocked(home, tmp_path):
    root, mem = home
    (tmp_path / "secret.txt").write_text("nope")
    (root / "link.txt").symlink_to(tmp_path / "secret.txt")
    tb, _ = toolbox(root, mem)
    with pytest.raises(ToolError):
        tb.read_file("link.txt")


def test_search_by_name_and_content(home):
    root, mem = home
    tb, _ = toolbox(root, mem)
    assert tb.search_files("invoice")["count"] == 1
    assert tb.search_files("*.md")["count"] == 1
    r = tb.search_files("priya contract", in_content=True)
    assert r["results"][0]["matched_in"] == "content"


def test_move_asks_and_verifies(home):
    root, mem = home
    tb, asked = toolbox(root, mem, answer=True)
    (root / "Tax").mkdir()
    r = tb.move_file("inbox/invoice_march.txt", "Tax")
    assert r["verified"] and (root / "Tax" / "invoice_march.txt").exists()
    assert "Move" in asked[0]


def test_declined_move_changes_nothing(home):
    root, mem = home
    tb, _ = toolbox(root, mem, answer=False)
    r = tb.move_file("inbox/invoice_march.txt", "archive.txt")
    assert r == {"done": False, "reason": "user declined"}
    assert (root / "inbox" / "invoice_march.txt").exists()


def test_write_refuses_silent_overwrite(home):
    root, mem = home
    tb, _ = toolbox(root, mem)
    with pytest.raises(ToolError):
        tb.write_file("notes.md", "oops")
    assert tb.write_file("new.txt", "hi")["verified"]


def test_unknown_and_private_tools_rejected(home):
    root, mem = home
    tb, _ = toolbox(root, mem)
    for name in ("_resolve", "call", "rm_rf"):
        with pytest.raises(ToolError):
            tb.call(name, {})


# ---- agent loop ------------------------------------------------------------
def test_full_task_search_move_remember_finish(home):
    root, mem = home
    (root / "Tax").mkdir()
    tb, _ = toolbox(root, mem)
    client = FakeClient([
        ([call("search_files", "c1", query="invoice")], ""),
        ([call("move_file", "c2", source=str(root / "inbox/invoice_march.txt"), destination=str(root / "Tax")),
          call("remember", "c3", key="tax folder", value=str(root / "Tax"))], ""),
        ([call("finish", "c4", status="done", summary="moved invoice_march.txt to Tax")], ""),
        ([], "Done. Obviously."),
    ])
    r = Agent(client, mem, tb, persona="smug").run("put my invoice in the tax folder")

    assert r.status == "done" and r.reply == "Done. Obviously."
    assert (root / "Tax" / "invoice_march.txt").exists()
    assert mem.facts()["tax folder"] == str(root / "Tax")
    task = mem.get_task(r.task_id)
    assert task.summary == "moved invoice_march.txt to Tax"
    kinds = [s["kind"] for s in mem.steps(r.task_id)]
    assert kinds[0] == "plan" and kinds[-1] == "reply" and "tool_result" in kinds

    # results go back with previous_response_id and matching call_ids
    second = client.requests[1]
    assert second["previous_response_id"] == "resp_1"
    assert second["input"][0]["call_id"] == "c1"
    # memory briefing reaches the model
    assert "What you know about the user" in client.requests[0]["instructions"]


def test_next_task_sees_what_was_done(home):
    root, mem = home
    tb, _ = toolbox(root, mem)
    Agent(FakeClient([([call("finish", "a", status="done", summary="sorted downloads")], ""),
                      ([], "ok")]), mem, tb).run("sort downloads")
    client = FakeClient([([], "You already sorted downloads.")])
    Agent(client, mem, tb).run("what did you do earlier?")
    assert "sorted downloads" in client.requests[0]["instructions"]


def test_tool_error_is_fed_back_not_crashed(home):
    root, mem = home
    tb, _ = toolbox(root, mem)
    client = FakeClient([
        ([call("read_file", "c1", path="/etc/passwd")], ""),
        ([call("finish", "c2", status="failed", summary="not allowed")], ""),
        ([], "Can't touch that."),
    ])
    r = Agent(client, mem, tb).run("read passwd")
    assert r.status == "failed"
    sent = json.loads(client.requests[1]["input"][0]["output"])
    assert "outside the folders" in sent["error"]


def test_bad_json_arguments_handled(home):
    root, mem = home
    tb, _ = toolbox(root, mem)
    bad = NS(type="function_call", name="list_dir", call_id="c1", arguments="{not json")
    client = FakeClient([([bad], ""), ([], "sorry")])
    r = Agent(client, mem, tb).run("list")
    assert "could not parse" in client.requests[1]["input"][0]["output"]
    assert r.status == "done"


def test_step_budget_marks_failed(home):
    root, mem = home
    tb, _ = toolbox(root, mem)
    loop = [([call("list_dir", f"c{i}")], "") for i in range(5)]
    r = Agent(FakeClient(loop), mem, tb, max_steps=3).run("loop forever")
    assert r.status == "failed" and mem.get_task(r.task_id).status == "failed"


def test_plain_chat_reply_finishes_task(home):
    root, mem = home
    tb, _ = toolbox(root, mem)
    r = Agent(FakeClient([([], "Hey. What do you need?")]), mem, tb).run("hi")
    assert r.status == "done" and mem.active_tasks() == []
