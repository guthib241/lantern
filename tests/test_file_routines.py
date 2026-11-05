"""S5: file tasks compile into routines whose steps are rules by pattern, not fixed paths. "Tidy my Downloads" done
once by the agent becomes "*.jpg -> Images, *.txt -> Text, ..."; the next time, whatever is in Downloads is sorted
by those rules with one approval that lists the exact moves, zero model calls, every move checked and undoable.
Every model is scripted; the files and the approvals are real."""
import json

import pytest

from fakes import call
from lantern.core import Core
from lantern.policy import WRITABLE, Policy, Rule
from lantern.routines.checker import check
from lantern.routines.contract import ContractError, skeleton_from_moves, validate
from lantern.routines.runner import execute, plan_for, render
from lantern.routines.runtime import RoutineContext
from lantern.routines.store import Routine
from test_routines import Both, Chat, code_answer
from test_scheduler import Routed, config, finish


def tidy_contract(kw):
    skel = json.loads(kw["messages"][1]["content"])
    return {"name": "tidy-downloads", "title": "Tidy Downloads",
            "examples": ["tidy my Downloads", "sort my downloads folder"],
            "steps": [{"id": s["id"], "say": s["say"]} for s in skel["steps"]], "new_params": []}


def make(folder, names):
    folder.mkdir(parents=True, exist_ok=True)
    for n in names:
        (folder / n).write_text(n)


# ---- the contract ------------------------------------------------------------------------------
def test_moves_become_rules_by_pattern(tmp_path):
    d = tmp_path / "Downloads"
    moves = [{"from": str(d / "a.jpg"), "to": str(d / "Images" / "a.jpg")},
             {"from": str(d / "b.JPG"), "to": str(d / "Images" / "b.JPG")},
             {"from": str(d / "notes.txt"), "to": str(d / "Text" / "notes.txt")},
             {"from": str(d / "todo.txt"), "to": str(tmp_path / "Work" / "todo.txt")},    # .txt went two ways
             {"from": str(d / "bill.pdf"), "to": str(tmp_path / "Documents" / "Bills" / "bill.pdf")}]
    c = skeleton_from_moves("[whatsapp] tidy my Downloads", moves, {"task_id": 3})
    assert validate(c) == [] and check(render(c)) == []
    rules = [(s["args"]["match"], s["args"]["to"]) for s in c["steps"]]
    assert ("*.jpg", "Images") in rules                                    # by extension, case folded
    assert ("notes.txt", "Text") in rules and ("todo.txt", str(tmp_path / "Work")) in rules   # by name: no one rule
    assert ("*.pdf", str(tmp_path / "Documents" / "Bills")) in rules       # outside the folder: the full path
    assert c["params"] == [{"name": "folder", "type": "path", "default": str(d), "say": "the folder to tidy"}]
    assert c["examples"] == ["tidy my Downloads"] and c["kind"] == "files"
    with pytest.raises(ContractError, match="more than one folder"):
        skeleton_from_moves("tidy", moves + [{"from": str(tmp_path / "x.jpg"), "to": str(d / "Images" / "x.jpg")}], {})
    with pytest.raises(ContractError, match="renamed"):
        skeleton_from_moves("tidy", [{"from": str(d / "a.jpg"), "to": str(d / "Images" / "b.jpg")}], {})


def test_the_plan_lists_the_exact_moves_and_the_run_does_only_those(tmp_path):
    d = tmp_path / "Downloads"
    make(d, ["a.jpg", "b.txt", "c.mp3", ".hidden.jpg"])
    make(d / "Images", ["old.jpg"])
    policy = Policy([Rule.make(tmp_path, WRITABLE)])
    c = skeleton_from_moves("tidy my Downloads", [{"from": str(d / "x.jpg"), "to": str(d / "Images" / "x.jpg")},
                                                  {"from": str(d / "y.txt"), "to": str(d / "Text" / "y.txt")}], {})
    r = Routine(c["name"], 1, c, render(c), {})
    plan = plan_for(r, {"folder": str(d)}, {}, target=str(d), policy=policy)
    assert plan["moves"] == [[str(d / "a.jpg"), str(d / "Images" / "a.jpg")], [str(d / "b.txt"), str(d / "Text" / "b.txt")]]
    assert "move 2 files" in plan["summary"] and plan["payload"]["moves"] == plan["moves"]
    make(d, ["late.jpg"])                                                  # arrives after the card was shown
    committed = []
    ctx = RoutineContext(mode="live", policy=policy, plan=plan, approve=lambda *a: (lambda payload: committed.append(payload)),
                         on_moved=lambda m: None)
    res = execute(r, {"folder": str(d)}, ctx)
    assert res.ok, res.failed
    assert sorted(p.name for p in (d / "Images").iterdir()) == ["a.jpg", "old.jpg"]
    assert (d / "late.jpg").exists() and (d / "c.mp3").exists() and (d / ".hidden.jpg").exists()
    assert res.left == [str(d / "late.jpg")] and len(res.moved) == 2 and committed == [plan["payload"]]


# ---- the whole story -------------------------------------------------------------------------------
def test_tidy_once_then_the_learned_rules_sort_new_files_with_no_model_calls(tmp_path):
    cfg = config(tmp_path)
    docs = tmp_path / "docs"
    down = docs / "Downloads"
    make(down, ["beach.jpg", "cat.png", "notes.txt", "invoice.pdf"])
    responses = Routed({"Downloads": [
        ([call("organize_files", "t1", moves=[
            {"source": str(down / "beach.jpg"), "destination": str(down / "Images") + "/"},
            {"source": str(down / "cat.png"), "destination": str(down / "Images") + "/"},
            {"source": str(down / "notes.txt"), "destination": str(down / "Text") + "/"},
            {"source": str(down / "invoice.pdf"), "destination": str(docs / "Documents" / "Invoices") + "/"}])], ""),
        finish("t2", "tidied Downloads"), ([], "Tidy."),
    ]})
    chat = Chat(routine_contract=tidy_contract, routine_code=code_answer)
    core = Core(cfg, Both(responses, chat), reminder_tick=0, workers=2)
    events, approvals = [], []
    core.subscribe(events.append)

    def tap(kind, a):
        if kind == "requested":
            approvals.append((a.action, a.summary))
            if a.action == "run_routine":
                make(down, ["late.png"])                               # lands while the card is on screen
            core.bus.decide(a.id, a.nonce, True, by="test")
    core.bus.subscribe(tap)

    first = core.submit("tidy my Downloads")                           # 1. the agent, with one approval
    assert core.wait_idle(30) and core.memory.get_task(first).status == "done"
    assert any(e["type"] == "learn_offer" and e["task_id"] == first for e in events)   # file tasks are learnable now
    core.learn(first)
    assert core.wait_idle(30)
    [row] = core.routines()
    assert row["name"] == "tidy-downloads" and row["trusted"], row
    r = core.compiler.store.load("tidy-downloads")
    assert check(r.code) == [] and "files.sort(" in r.code and str(down) not in r.code   # the folder is a param

    make(down, ["sunset.jpg", "todo.txt", "song.mp3", "receipt.pdf"])   # 2. a new week's mess
    approvals.clear()
    second = core.submit("tidy my Downloads")
    assert core.wait_idle(30)
    t2 = core.memory.get_task(second)
    assert t2.status == "done" and "0 model calls" in t2.summary and "Moved 3 files" in t2.summary, t2.summary
    assert "1 arrived after you approved" in t2.summary
    assert core.memory.task_usage(second)["calls"] == 0
    [(action, summary)] = approvals
    assert action == "run_routine" and "move 3 files" in summary
    assert (down / "Images" / "sunset.jpg").exists() and (down / "Text" / "todo.txt").exists()
    assert (docs / "Documents" / "Invoices" / "receipt.pdf").exists()
    assert (down / "song.mp3").exists() and (down / "late.png").exists()   # no rule; not approved

    undo = core.undo(second)                                           # 3. Undo puts it all back
    assert core.wait_idle(30) and core.memory.get_task(undo).status == "done"
    assert all((down / n).exists() for n in ("sunset.jpg", "todo.txt", "receipt.pdf"))
    core.shutdown()
