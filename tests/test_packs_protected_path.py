"""Agent packs on the protected path: with Lantern's scheduler (lantern --serve), a pack's run takes the same road
as every other task: the S0 planner/reader split, the model ladder and parking. Its instructions, its tool
allowlist and its taint come along, and the allowlist survives a pause for an approval.

These came from combining two lines of work: packs (claude/freecad-laptop) were written for the one-at-a-time
agent, the scheduler (claude/phase2-core) arrived on the other branch. All model calls are scripted."""
import json
import time

from fakes import FakeClient, call
from lantern.config import Config
from lantern.core import Core
from lantern.security import S0_RULES

PACK = {"format": "lantern-agent/1", "name": "hello-pack", "title": "Hello", "description": "Says hi.",
        "author": "bea", "version": "1.0.0",
        "inputs": [{"key": "who", "question": "Who to greet?", "example": "Priya"}],
        "goal": "Say hello to {who}.", "instructions": "Just answer in one line.",
        "tools": ["list_folders"]}


def make(tmp_path, script):
    root = tmp_path / "docs"
    root.mkdir(exist_ok=True)
    cfg = Config(roots=[str(root)], db_path=str(tmp_path / "m.db"))
    cfg.audit_path, cfg.zones_file = str(tmp_path / "audit.db"), str(tmp_path / "zones.json")
    cfg.agents_dir = str(tmp_path / "agents")
    fake = FakeClient(script)
    core = Core(cfg, fake, reminder_tick=0, workers=1)
    return core, fake, root


def results(core, task_id):
    return [json.loads(s["content"])["result"] for s in core.memory.steps(task_id) if s["kind"] == "tool_result"]


def until(check, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if check():
            return True
        time.sleep(0.01)
    raise AssertionError("timed out")


def test_a_pack_runs_through_the_scheduler_with_its_rules_and_allowlist(tmp_path):
    core, fake, _ = make(tmp_path, [([call("search_files", "c1", query="x")], ""),
                                    ([call("finish", "c2", status="done", summary="said hi")], ""),
                                    ([], "Hi Priya.")])
    try:
        assert core.scheduler is not None
        core.packs.install(PACK)
        tid = core.submit("", pack="hello-pack", inputs={"who": "Priya"})
        until(lambda: core.memory.get_task(tid).status in ("done", "failed"))
        assert core.memory.get_task(tid).status == "done"
        assert results(core, tid)[0]["error"] == "this agent pack may not use search_files"   # (+ S2 recovery hint)
        first = fake.requests[0]["instructions"]
        assert S0_RULES.strip()[:60] in first          # the planner of the protected path, not the plain agent
        assert "Just answer in one line." in first     # the pack's own instructions come last
    finally:
        core.shutdown()


def test_the_allowlist_survives_a_pause_for_an_approval(tmp_path):
    """write_file waits for the user's OK (the task parks and gives up its slot); after the OK, a tool outside the
    pack is still refused: the allowlist was saved with the task, not only kept on one toolbox."""
    pack = {**PACK, "tools": ["write_file"]}
    core, _, root = make(tmp_path, [([call("write_file", "c1", path=str(tmp_path / "docs" / "hi.txt"), content="hi")],
                                     ""),
                                    ([call("list_folders", "c2")], ""),
                                    ([call("finish", "c3", status="done", summary="wrote it")], ""),
                                    ([], "Wrote it.")])
    try:
        core.packs.install(pack)
        tid = core.submit("", pack="hello-pack", inputs={"who": "Priya"})
        until(lambda: bool(core.bus.pending()))
        a = core.bus.pending()[0]
        assert a.action == "write_file" and a.tainted is True   # a shared pack starts untrusted
        core.bus.decide(a.id, a.nonce, True, by="test")
        until(lambda: core.memory.get_task(tid).status in ("done", "failed"))
        got = results(core, tid)
        assert got[1]["error"] == "this agent pack may not use list_folders"
        assert (root / "hi.txt").read_text() == "hi"
    finally:
        core.shutdown()


def test_a_normal_task_after_a_pack_has_every_tool_again(tmp_path):
    core, _, _ = make(tmp_path, [([call("finish", "c1", status="done", summary="said hi")], ""), ([], "Hi.")] +
                      [([call("list_folders", "d1")], ""), ([call("finish", "d2", status="done", summary="listed")], ""),
                       ([], "Listed.")])
    try:
        core.packs.install({**PACK, "tools": ["write_file"]})
        first = core.submit("", pack="hello-pack", inputs={"who": "Priya"})
        until(lambda: core.memory.get_task(first).status == "done")
        second = core.submit("list my folders")
        until(lambda: core.memory.get_task(second).status in ("done", "failed"))
        assert "allowed_folders" in results(core, second)[0]
    finally:
        core.shutdown()


def test_the_allowlist_survives_a_question_to_the_user(tmp_path):
    """ask_user parks the task; the answer resumes it from its saved state, where begin_task resets the toolbox.
    The pack's allowlist comes back from that state, so after the answer a tool outside the pack is still refused."""
    pack = {**PACK, "tools": ["ask_user"]}
    core, _, _ = make(tmp_path, [([call("ask_user", "c1", question="Which Priya?", options=["work", "home"])], ""),
                                 ([call("list_folders", "c2")], ""),
                                 ([call("finish", "c3", status="done", summary="asked")], ""),
                                 ([], "Done.")])
    try:
        core.packs.install(pack)
        tid = core.submit("", pack="hello-pack", inputs={"who": "Priya"})
        until(lambda: bool(core.questions()))
        core.answer(tid, "work", by="test")
        until(lambda: core.memory.get_task(tid).status in ("done", "failed"))
        got = [r for r in results(core, tid) if "error" in r]
        assert got and got[0]["error"] == "this agent pack may not use list_folders"
    finally:
        core.shutdown()
