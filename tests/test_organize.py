"""M19: tidy a folder. Many moves, one approval bound to the whole plan, each move verified."""
import json

import pytest

from fakes import FakeClient, call
from lantern import demo
from lantern.agent import Agent
from lantern.approvals import ApprovalBus, CallbackGate
from lantern.memory import Memory
from lantern.policy import Policy, PolicyError, Rule
from lantern.tools import Toolbox, ToolError


def setup(tmp_path, answer=True, before_yes=None):
    root = tmp_path / "home"
    dl = root / "Downloads"
    dl.mkdir(parents=True)
    for name, data in {"a.jpg": b"jpg", "b.png": b"png!", "c.pdf": b"%PDF", "d.txt": b"notes"}.items():
        (dl / name).write_bytes(data)
    mem = Memory(tmp_path / "m.db")
    bus = ApprovalBus()
    asked = []

    def decide(summary):
        asked.append(summary)
        if before_yes:
            before_yes(dl)
        return answer
    tb = Toolbox(Policy.from_roots([root]), mem, CallbackGate(decide, bus))
    return tb, dl, asked, bus


def plan(dl):
    return [{"source": str(dl / "a.jpg"), "destination": str(dl / "Images") + "/"},
            {"source": str(dl / "b.png"), "destination": str(dl / "Images") + "/"},
            {"source": str(dl / "c.pdf"), "destination": str(dl / "Docs" / "invoice.pdf")}]


def test_one_approval_moves_everything_and_verifies(tmp_path):
    tb, dl, asked, bus = setup(tmp_path)
    r = tb.organize_files(plan(dl))
    assert r["done"] and r["verified"] and r["moved"] == 3 and r["failed"] == []
    assert len(asked) == 1 and asked[0].startswith("Move 3 files: 2 → home/Downloads/Images; 1 → home/Downloads/Docs")
    assert str(tmp_path) not in asked[0]   # readable paths on the card; the payload keeps the full ones
    assert sorted(p.name for p in (dl / "Images").iterdir()) == ["a.jpg", "b.png"]
    assert (dl / "Docs" / "invoice.pdf").read_bytes() == b"%PDF" and not (dl / "c.pdf").exists()
    a = list(bus._items.values())[0]
    assert a.action == "organize_files" and a.status == "used" and len(a.payload["moves"]) == 3
    # the undo plan puts things back (with another approval)
    back = tb.organize_files(r["undo"])
    assert back["verified"] and (dl / "c.pdf").exists() and not (dl / "Images" / "a.jpg").exists()


def test_declined_moves_nothing(tmp_path):
    tb, dl, asked, _ = setup(tmp_path, answer=False)
    assert tb.organize_files(plan(dl)) == {"done": False, "reason": "user declined"}
    assert (dl / "a.jpg").exists() and not (dl / "Images").exists()


def test_file_changed_after_approval_is_refused(tmp_path):
    tb, dl, asked, _ = setup(tmp_path, before_yes=lambda d: (d / "a.jpg").write_bytes(b"swapped by malware"))
    r = tb.organize_files(plan(dl))
    assert r["done"] is False and "changed" in r["reason"]
    assert (dl / "a.jpg").exists() and not (dl / "Images").exists()


@pytest.mark.parametrize("bad, why", [
    ([], "empty"),
    ([{"source": "Downloads/a.jpg"}], "source and a destination"),
    ([{"source": "Downloads/nope.jpg", "destination": "Downloads/x/"}], "not a file"),
    ([{"source": "Downloads", "destination": "Elsewhere/"}], "not a file"),
    ([{"source": "Downloads/a.jpg", "destination": "Downloads/d.txt"}], "already exists"),
    ([{"source": "Downloads/a.jpg", "destination": "Downloads/X/"},
      {"source": "Downloads/a.jpg", "destination": "Downloads/Y/"}], "twice"),
    ([{"source": "Downloads/a.jpg", "destination": "/etc/a.jpg"}], "outside the folders"),
])
def test_bad_plans_are_refused_before_asking(tmp_path, bad, why):
    tb, dl, asked, _ = setup(tmp_path)
    with pytest.raises(ToolError, match=why):
        tb.organize_files(bad)
    assert asked == []


def test_moving_a_folder_never_carries_a_private_folder_out_of_its_zone(tmp_path):
    root = tmp_path / "home"
    (root / "Work" / "Secret").mkdir(parents=True)
    (root / "Work" / "Secret" / "x.txt").write_text("private")
    policy = Policy([Rule.make(root, "writable"), Rule.make(root / "Work" / "Secret", "private")])
    with pytest.raises(PolicyError, match="contains a private folder"):
        policy.move(root / "Work", root / "Archive")
    tb = Toolbox(policy, Memory(tmp_path / "m.db"), lambda s: True)
    with pytest.raises(ToolError, match="private folder"):
        tb.move_file(str(root / "Work"), str(root / "Archive"))
    assert (root / "Work" / "Secret" / "x.txt").exists()


def test_agent_tidies_with_one_card(tmp_path):
    tb, dl, asked, _ = setup(tmp_path)
    fake = FakeClient([
        ([call("organize_files", "c1", moves=plan(dl))], ""),
        ([call("finish", "c2", status="done", summary="tidied Downloads")], ""),
        ([], "Tidied. You're welcome."),
    ])
    r = Agent(fake, tb.memory, tb).run("tidy my downloads")
    assert r.status == "done" and len(asked) == 1
    out = json.loads(fake.requests[1]["input"][0]["output"])
    assert out["verified"] and out["moved"] == 3


def test_rehearsal_tidies_downloads(tmp_path):
    cfg = demo.prepare(tmp_path / "rehearsal")
    core, hub, phone = demo.build(cfg, locate=lambda: {})
    hub.start(listen=False)
    lines = []
    try:
        result = demo.run_story(core, hub, phone, out=lines.append, beats=[demo.Beat("Tidy my Downloads folder.")])
    finally:
        hub.stop()
        core.shutdown()
    assert result["beats"][0]["status"] == "done", lines
    home = tmp_path / "rehearsal" / "home" / "Downloads"
    assert sorted(p.name for p in (home / "Images").iterdir()) == ["IMG_2041.jpg", "Screenshot 2026-10-20 at 11.02.png"]
    assert (home / "Spreadsheets" / "team_budget.csv").exists() and (home / "Documents" / "notes_from_call.txt").exists()
    assert "Moved 6 files" in result["beats"][0]["reply"]
    assert not (demo.DEMO_HOME / "Downloads" / "Images").exists()   # the repo's copy is untouched
