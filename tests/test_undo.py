"""M29: Undo. Put back what a task moved, from its own verified log: one approval, checked on disk, no model."""
import pytest
from fastapi.testclient import TestClient

from fakes import FakeClient, call
from lantern.channels import ChannelHub, FakeChannel
from lantern.config import Config
from lantern.core import Core
from lantern.memory import Memory
from lantern.policy import Policy
from lantern.server import create_app
from lantern.tools import Toolbox
from lantern.undo import UndoError, undo_plan, undoable


def home(tmp_path):
    root = tmp_path / "home"
    dl = root / "Downloads"
    dl.mkdir(parents=True)
    for name, data in {"a.jpg": b"jpg", "b.png": b"png!", "c.pdf": b"%PDF"}.items():
        (dl / name).write_bytes(data)
    return root, dl


def tidy_plan(dl):
    return [{"source": str(dl / "a.jpg"), "destination": str(dl / "Images") + "/"},
            {"source": str(dl / "b.png"), "destination": str(dl / "Images") + "/"},
            {"source": str(dl / "c.pdf"), "destination": str(dl / "Docs" / "invoice.pdf")}]


def tidy_script(dl):
    return [([call("organize_files", "c1", moves=tidy_plan(dl))], ""),
            ([call("finish", "c2", status="done", summary="tidied")], ""),
            ([], "Tidied. Obviously.")]


def make_core(tmp_path, answer=True):
    """A Core scripted to tidy Downloads once, whose cards are answered at once (yes, or `answer(card)`)."""
    root, dl = home(tmp_path)
    cfg = Config(roots=[str(root)], db_path=str(tmp_path / "m.db"))
    cfg.audit_path, cfg.zones_file = str(tmp_path / "audit.db"), str(tmp_path / "zones.json")
    cfg.media_dir = str(tmp_path / "media")
    fake = FakeClient(tidy_script(dl))
    core = Core(cfg, fake, approval_timeout=5, reminder_tick=0)
    core.fake, core.events, core.cards = fake, [], []

    def tap(e):
        core.events.append(e)
        if e["type"] == "approval" and e["event"] == "requested":
            a = e["approval"]
            core.cards.append(a)
            core.bus.decide(a["id"], a["nonce"], answer(a) if callable(answer) else answer)
    core.subscribe(tap)
    return core, dl


def test_undo_puts_everything_back_with_one_card_and_no_model(tmp_path):
    core, dl = make_core(tmp_path)
    tid = core.submit("tidy my downloads")
    assert core.wait_idle()
    assert (dl / "Images" / "a.jpg").exists() and core.memory.get_task(tid).status == "done"
    assert core.task_view(tid)["undoable"] is True
    sent_before = len(core.fake.requests)

    new = core.undo(tid)
    assert core.wait_idle()
    t = core.memory.get_task(new)
    assert t.status == "done" and t.summary == "Put 3 files back where they were. Each checked on disk."
    assert sorted(p.name for p in dl.iterdir() if p.is_file()) == ["a.jpg", "b.png", "c.pdf"]
    assert (dl / "c.pdf").read_bytes() == b"%PDF" and not (dl / "Docs" / "invoice.pdf").exists()
    assert (dl / "Images").is_dir()                          # empty folders stay: Lantern never deletes
    assert len(core.fake.requests) == sent_before   # no model call, $0
    assert core.cards[-1]["action"] == "undo_moves" and core.cards[-1]["summary"].startswith(f"Undo task #{tid}: put back 3 files")
    assert {"type": "undone", "from": tid, "task_id": new, "moved": 3}.items() <= \
        [e for e in core.events if e["type"] == "undone"][0].items()
    done = [e for e in core.events if e["type"] == "done" and e["id"] == new][0]
    assert done["cost_usd"] == 0.0 and done["status"] == "done"
    assert core.task_view(tid)["undoable"] is False          # undone once
    with pytest.raises(UndoError, match="already undone"):
        core.undo(tid)
    assert core.task_view(new)["undoable"] is True           # and the undo itself can be undone (a redo)
    core.shutdown()


def test_declined_undo_moves_nothing_and_try_again_asks_again(tmp_path):
    answers = iter([True, False, True])
    core, dl = make_core(tmp_path, answer=lambda a: next(answers))
    tid = core.submit("tidy my downloads")
    assert core.wait_idle()
    no = core.undo(tid)
    assert core.wait_idle()
    assert core.memory.get_task(no).status == "cancelled" and (dl / "Images" / "a.jpg").exists()
    assert core.task_view(tid)["undoable"] is True           # a "no" doesn't use the undo up
    again = core.retry_task(no)                               # runs as an undo again, not through the model
    assert core.wait_idle()
    assert core.memory.get_task(again).status == "done" and (dl / "a.jpg").exists()
    assert [c["action"] for c in core.cards] == ["organize_files", "undo_moves", "undo_moves"]
    core.shutdown()


def test_never_puts_a_file_back_over_a_new_one(tmp_path):
    core, dl = make_core(tmp_path)
    tid = core.submit("tidy")
    assert core.wait_idle()
    (dl / "c.pdf").write_bytes(b"a new download with the same name")
    new = core.undo(tid)
    assert core.wait_idle()
    t = core.memory.get_task(new)
    assert t.status == "failed" and "already exists" in t.summary
    assert (dl / "c.pdf").read_bytes() == b"a new download with the same name"
    assert (dl / "Images" / "a.jpg").exists()               # refused before asking: nothing moved
    assert [c["action"] for c in core.cards] == ["organize_files"]
    assert core.task_view(tid)["undoable"] is True
    core.shutdown()


def test_files_already_back_are_skipped(tmp_path):
    core, dl = make_core(tmp_path)
    tid = core.submit("tidy")
    assert core.wait_idle()
    (dl / "Images" / "a.jpg").rename(dl / "a.jpg")         # the user put one back by hand
    new = core.undo(tid)
    assert core.wait_idle()
    step = [s for s in core.memory.steps(new) if s["kind"] == "tool_result"][0]
    assert '"already_back": 1' in step["content"] and '"moved": 2' in step["content"]
    assert core.memory.get_task(new).status == "done" and (dl / "b.png").exists()
    core.shutdown()


def test_undo_respects_privacy_zones_changed_since(tmp_path):
    core, dl = make_core(tmp_path)
    tid = core.submit("tidy")
    assert core.wait_idle()
    core.policy.set_zone(dl / "Images", "open")   # read-only now
    new = core.undo(tid)
    assert core.wait_idle()
    assert core.memory.get_task(new).status == "failed" and (dl / "Images" / "a.jpg").exists()
    core.shutdown()


def test_plan_collapses_chains_and_round_trips(tmp_path):
    root, dl = home(tmp_path)
    mem = Memory(tmp_path / "m.db")
    tb = Toolbox(Policy.from_roots([root]), mem, lambda summary: True)
    t = mem.start_task("shuffle")
    tb.begin_task(t.id, "shuffle")
    assert tb.move_file(str(dl / "a.jpg"), str(dl / "x.jpg"))["verified"]
    assert tb.organize_files([{"source": str(dl / "x.jpg"), "destination": str(dl / "y.jpg")},
                              {"source": str(dl / "b.png"), "destination": str(dl / "B") + "/"}])["verified"]
    assert tb.move_file(str(dl / "c.pdf"), str(dl / "d.pdf"))["verified"]
    assert tb.move_file(str(dl / "d.pdf"), str(dl / "c.pdf"))["verified"]     # there and back: left alone
    mem.finish_task(t.id, "done", "ok")
    assert undo_plan(mem, t.id) == [{"source": str(dl / "B" / "b.png"), "destination": str(dl / "b.png")},
                                    {"source": str(dl / "y.jpg"), "destination": str(dl / "a.jpg")}]
    other = mem.start_task("just reading")
    mem.finish_task(other.id, "done", "ok")
    with pytest.raises(UndoError, match="didn't move any files"):
        undo_plan(mem, other.id)
    with pytest.raises(KeyError):
        undo_plan(mem, 999)
    assert undoable(mem, t.id) and not undoable(mem, other.id) and not undoable(mem, 999)


def test_a_moved_folder_goes_back(tmp_path):
    root, dl = home(tmp_path)
    (dl / "trip").mkdir()
    (dl / "trip" / "ticket.pdf").write_bytes(b"%PDF")
    mem = Memory(tmp_path / "m.db")
    asked = []
    tb = Toolbox(Policy.from_roots([root]), mem, lambda summary: asked.append(summary) or True)
    t = mem.start_task("move trip")
    tb.begin_task(t.id, "move trip")
    assert tb.move_file(str(dl / "trip"), str(root / "Trips") + "/")["verified"]
    mem.finish_task(t.id, "done", "ok")
    r = tb.undo_moves(undo_plan(mem, t.id), t.id)
    assert r["verified"] and (dl / "trip" / "ticket.pdf").read_bytes() == b"%PDF" and not (root / "Trips" / "trip").exists()
    assert asked[-1].startswith(f"Undo task #{t.id}: put back 1 file")
    assert "undo" not in r                                     # the model never gets an undo-of-an-undo plan


def test_undo_moves_is_not_a_model_tool(tmp_path):
    from lantern.tools import TOOL_NAMES, ToolError
    root, dl = home(tmp_path)
    mem = Memory(tmp_path / "m.db")
    tb = Toolbox(Policy.from_roots([root]), mem, lambda summary: True)
    assert "undo_moves" not in TOOL_NAMES
    with pytest.raises(ToolError, match="unknown tool"):
        tb.call("undo_moves", {"moves": [], "of_task": 1})


def test_server_and_phone(tmp_path):
    core, dl = make_core(tmp_path)
    phone = FakeChannel()
    hub = ChannelHub(core, [phone]).start(listen=False)
    c = TestClient(create_app(core, "tok", hub=hub))
    h = {"X-Lantern-Token": "tok"}
    hub.receive(phone.incoming("undo"))
    hub.wait_idle()
    assert phone.messages()[-1]["text"] == "Nothing to undo: none of your recent tasks moved files."
    tid = core.submit("tidy")
    assert core.wait_idle()
    assert c.post("/tasks/999/undo", headers=h).status_code == 404
    assert c.post(f"/tasks/{tid}/undo").status_code == 401
    assert [t["undoable"] for t in c.get("/tasks", headers=h).json()] == [True]
    hub.receive(phone.incoming("undo that"))
    hub.wait_idle()
    assert core.wait_idle()
    hub.wait_idle()
    texts = [m["text"] for m in phone.messages()]
    assert any(x.startswith("↩️ Undo is task #") for x in texts)
    assert "Put 3 files back where they were. Each checked on disk." in texts
    assert (dl / "a.jpg").exists()
    r = c.post(f"/tasks/{tid}/undo", headers=h)
    assert r.status_code == 409 and "already undone" in r.json()["detail"]
    hub.stop()
    core.shutdown()


def test_typing_undo_in_the_panel_undoes_the_last_task(tmp_path):
    core, dl = make_core(tmp_path)
    c = TestClient(create_app(core, "tok"))
    h = {"X-Lantern-Token": "tok"}
    r = c.post("/task", json={"goal": "undo"}, headers=h)
    assert r.status_code == 409 and r.json()["detail"].startswith("Nothing to undo")
    tid = core.submit("tidy")
    assert core.wait_idle()
    sent = len(core.fake.requests)
    new = c.post("/task", json={"goal": "Undo that."}, headers=h).json()["task_id"]
    assert core.wait_idle()
    assert core.memory.get_task(new).goal == f"Undo task #{tid}: put back 3 files" and (dl / "a.jpg").exists()
    assert len(core.fake.requests) == sent                      # the word never went to the model
    # from another program over MCP it's just text for the model, never a shortcut
    core.fake.script.append(([], "I can't undo things for other programs."))
    mcp = c.post("/task", json={"goal": "undo", "source": "mcp"}, headers=h).json()["task_id"]
    assert core.wait_idle()
    assert core.memory.get_task(mcp).goal == "[mcp] undo" and len(core.fake.requests) == sent + 1
    core.shutdown()


def test_a_partial_undo_can_be_finished_later(tmp_path, monkeypatch):
    core, dl = make_core(tmp_path)
    tid = core.submit("tidy")
    assert core.wait_idle()
    real_move, calls = core.policy.move, []

    def flaky(src, dst):
        calls.append(src)
        if len(calls) == 2:
            raise OSError("disk hiccup")
        return real_move(src, dst)
    monkeypatch.setattr(core.policy, "move", flaky)
    first = core.undo(tid)
    assert core.wait_idle()
    t = core.memory.get_task(first)
    assert t.status == "failed" and t.summary == "Put 1 of 3 back, then stopped: disk hiccup"
    assert core.task_view(tid)["undoable"] is True              # not used up: the next try finishes the job
    second = core.undo(tid)
    assert core.wait_idle()
    t = core.memory.get_task(second)
    assert t.status == "done" and t.summary == "Put 3 files back where they were. Each checked on disk."
    assert sorted(p.name for p in dl.iterdir() if p.is_file()) == ["a.jpg", "b.png", "c.pdf"]
    assert core.task_view(tid)["undoable"] is False
    core.shutdown()


def test_typed_undo_waits_for_a_running_task(tmp_path):
    core, dl = make_core(tmp_path)
    tid = core.submit("tidy")
    assert core.wait_idle()
    core.current_task = 12345                                 # something else is mid-run
    with pytest.raises(UndoError, match="still running"):
        core.undo_last()
    core.current_task = None
    assert core.memory.get_task(core.undo_last()).goal == f"Undo task #{tid}: put back 3 files"
    with pytest.raises(UndoError, match="already queued|already undone"):
        core.undo(tid)                                         # a double tap doesn't queue a second undo
    assert core.wait_idle()
    core.shutdown()
