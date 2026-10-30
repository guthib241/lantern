"""T3a: agent packs (shareable agents as data-only files): validation, store,
install with approval, runs with a tool allowlist, drafts, server routes."""
import hashlib
import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fakes import FakeClient, call
from lantern import models
from lantern.agentpacks import (
    MAX_BYTES,
    PackError,
    PackStore,
    canonical_bytes,
    pack_suffix,
    tool_allowed,
    validate,
)
from lantern.config import Config
from lantern.core import Core
from lantern.server import create_app

ROOT = Path(__file__).resolve().parent.parent


def good_pack(**over) -> dict:
    pack = {"format": "lantern-agent/1", "name": "tidy-test", "title": "Tidy test",
            "description": "Sorts a folder into subfolders.", "author": "bea", "version": "1.0.0",
            "inputs": [{"key": "folder", "question": "Which folder?", "example": "Downloads"}],
            "goal": "Tidy {folder} into subfolders.",
            "instructions": "List the folder, then call organize_files once with the whole plan.",
            "tools": ["list_folders", "list_dir", "organize_files"]}
    pack.update(over)
    return pack


def make_core(tmp_path, script, approval_timeout=30):
    root = Path(tmp_path) / "docs"
    root.mkdir(parents=True, exist_ok=True)
    cfg = Config(roots=[str(root)], db_path=str(tmp_path / "m.db"))
    cfg.audit_path = str(tmp_path / "audit.db")
    cfg.zones_file = str(tmp_path / "zones.json")
    cfg.agents_dir = str(tmp_path / "agents")
    fake = FakeClient(script)
    core = Core(cfg, fake, approval_timeout=approval_timeout)
    return core, root, fake


def pending(core, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        found = core.bus.pending()
        if found:
            return found[0]
        time.sleep(0.01)
    raise AssertionError("no approval was requested")


# ---- validation ---------------------------------------------------------------
def test_good_pack_ok_and_examples_validate():
    pack = validate(good_pack())
    assert pack.render_goal({"folder": "Downloads"}) == "Tidy Downloads into subfolders."
    for f in ("cad-enclosure", "tidy-downloads"):
        data = json.loads((ROOT / "agents" / f"{f}.lantern-agent.json").read_text(encoding="utf-8"))
        validate(data)


def test_unknown_key_refused():
    with pytest.raises(PackError, match="unknown field"):
        validate(good_pack(surprise="hi"))


@pytest.mark.parametrize("name", ["Bad Name", "-lead", "UPPER", "a" * 42, "", 7])
def test_bad_name_refused(name):
    with pytest.raises(PackError, match="name"):
        validate(good_pack(name=name))


def test_model_field_refused():
    with pytest.raises(PackError, match="model"):
        validate(good_pack(model="muse-spark-1.3"))


def test_unknown_tool_refused_but_finish_and_mcp_patterns_ok():
    with pytest.raises(PackError, match="unknown tool"):
        validate(good_pack(tools=["teleport"]))
    with pytest.raises(PackError, match="non-empty"):
        validate(good_pack(tools=[]))
    validate(good_pack(tools=["finish", "freecad.*", "notes.search"]))


def test_goal_placeholder_without_input_refused():
    with pytest.raises(PackError, match="no such input"):
        validate(good_pack(goal="Tidy {nowhere} please."))
    with pytest.raises(PackError, match="at most 6"):
        validate(good_pack(inputs=[{"key": f"k{i}", "question": "q"} for i in range(7)]))


def test_over_32kb_refused():
    big = good_pack(instructions="x" * (MAX_BYTES + 1))
    assert len(json.dumps(big, ensure_ascii=False).encode()) > MAX_BYTES
    with pytest.raises(PackError, match="32 KB"):
        validate(big)


def test_api_key_in_instructions_refused():
    with pytest.raises(PackError, match="secret"):
        validate(good_pack(instructions="Use the key sk-ant-abcdefghij1234567890 to tidy."))


def test_render_goal_checks_values():
    pack = validate(good_pack())
    with pytest.raises(PackError, match="needs a value"):
        pack.render_goal({})
    with pytest.raises(PackError, match="no input called"):
        pack.render_goal({"folder": "D", "extra": "x"})
    with pytest.raises(PackError, match="too long"):
        pack.render_goal({"folder": "x" * 501})
    assert pack.render_goal({"folder": "a{b}c"}) == "Tidy abc into subfolders."


def test_allowlist_matching():
    assert tool_allowed(None, "anything")
    assert tool_allowed({"search_files"}, "search_files")
    assert tool_allowed({"search_files"}, "finish")
    assert tool_allowed({"freecad.*"}, "freecad.box")
    assert not tool_allowed({"freecad.*"}, "write_file")
    assert not tool_allowed({"search_files"}, "write_file")


# ---- install ------------------------------------------------------------------
def test_store_install_replace_disable_enable_export(tmp_path):
    store = PackStore(tmp_path / "agents")
    assert store.list() == []
    stored = store.install(good_pack())
    assert stored.name == "tidy-test" and not stored.disabled
    assert [p["name"] for p in store.list()] == ["tidy-test"]
    with pytest.raises(PackError, match="replace=True"):
        store.install(good_pack(author="cal"))
    store.install(good_pack(author="cal", title="T2"), replace=True)
    assert store.get("tidy-test").author == "cal"
    store.disable("tidy-test")
    assert store.get("tidy-test").disabled
    assert store.list()[0]["disabled"]
    with pytest.raises(KeyError):
        store.get("missing")
    store.enable("tidy-test")
    assert not store.get("tidy-test").disabled
    raw = store.export("tidy-test")
    assert json.loads(raw) == {**good_pack(author="cal", title="T2")}
    assert store.export("tidy-test") == raw  # byte-stable


def install_in_thread(core, data, replace=False):
    out = {}
    t = threading.Thread(target=lambda: out.update(core.pack_install(data, replace=replace)))
    t.start()
    return t, out


def test_install_approval_card_lists_tools_and_binds_hash(tmp_path):
    core, _, _ = make_core(tmp_path, [])
    try:
        t, out = install_in_thread(core, good_pack())
        a = pending(core)
        assert a.action == "install_agent"
        for tool in good_pack()["tools"]:
            assert tool in a.summary
        assert "Tidy test" in a.summary and "bea" in a.summary and "1.0.0" in a.summary
        assert good_pack()["instructions"] in a.summary
        assert a.payload["sha256"] == hashlib.sha256(canonical_bytes(validate(good_pack()).as_dict())).hexdigest()
        core.bus.decide(a.id, a.nonce, True, by="test", expected_hash=a.payload_hash)
        t.join(timeout=10)
        assert out == {"installed": True, "verified": True, "name": "tidy-test"}
        assert core.packs.get("tidy-test").title == "Tidy test"
    finally:
        core.shutdown()


def test_install_decline_writes_nothing(tmp_path):
    core, _, _ = make_core(tmp_path, [])
    try:
        t, out = install_in_thread(core, good_pack())
        a = pending(core)
        core.bus.decide(a.id, a.nonce, False, by="test")
        t.join(timeout=10)
        assert out["installed"] is False
        assert core.packs.list() == []
    finally:
        core.shutdown()


def test_install_invalid_asks_no_approval(tmp_path):
    core, _, _ = make_core(tmp_path, [])
    try:
        with pytest.raises(PackError):
            core.pack_install(good_pack(model="muse-spark-1.3"))
        assert core.bus.pending() == []
    finally:
        core.shutdown()


# ---- running ------------------------------------------------------------------
TINY = {"format": "lantern-agent/1", "name": "hello-pack", "title": "Hello", "description": "Says hi.",
        "author": "bea", "version": "1.0.0",
        "inputs": [{"key": "who", "question": "Who to greet?", "example": "Priya"}],
        "goal": "Say hello to {who}.", "instructions": "Just answer in one line.",
        "tools": ["list_folders"]}


def finish_script(summary, reply="done."):
    return [([call("finish", "c1", status="done", summary=summary)], ""), ([], reply)]


def test_run_renders_goal_and_appends_instructions_after_now(tmp_path):
    # Two fresh cores: a later task's briefing mentions earlier tasks, so a shared
    # core can't show the prefix is identical. Fresh memories make the heads comparable.
    core_n, _, fake_n = make_core(tmp_path / "n", finish_script("hi"))
    core_p, _, fake_p = make_core(tmp_path / "p", finish_script("hi"))
    try:
        core_p.packs.install(good_pack())
        normal = core_n.submit("say hi")
        assert core_n.wait_idle()
        pack_id = core_p.submit("", pack="tidy-test", inputs={"folder": "Downloads"})
        assert core_p.wait_idle()
        assert core_p.memory.get_task(pack_id).goal == "[agent:tidy-test] Tidy Downloads into subfolders."
        first = fake_n.requests[0]["instructions"]
        second = fake_p.requests[0]["instructions"]
        head_first, _, _ = first.partition("Now: ")
        head_second, _, tail = second.partition("Now: ")
        # The only difference is the briefing's "in progress" line, which names each task's own goal.
        pack_goal = "[agent:tidy-test] Tidy Downloads into subfolders."
        assert head_first.replace("say hi", "GOAL") == head_second.replace(pack_goal, "GOAL")
        assert tail.strip().endswith(pack_suffix(core_p.packs.get("tidy-test")))
        assert "Agent pack" in tail and "organize_files once" in tail
        assert core_n.memory.get_task(normal).goal == "say hi"
    finally:
        core_n.shutdown()
        core_p.shutdown()


def test_run_refuses_tool_outside_the_pack(tmp_path):
    core, _, _ = make_core(tmp_path, [([call("search_files", "c1", query="x")], ""),
                                   ([call("finish", "c2", status="done", summary="nope")], ""),
                                   ([], "couldn't.")])
    try:
        core.packs.install(TINY)
        tid = core.submit("", pack="hello-pack", inputs={"who": "Priya"})
        assert core.wait_idle()
        results = [json.loads(s["content"])["result"]
                   for s in core.memory.steps(tid) if s["kind"] == "tool_result"]
        assert results[0] == {"error": "this agent pack may not use search_files"}
    finally:
        core.shutdown()


def test_run_starts_tainted_and_needs_approvals(tmp_path):
    core, _, _ = make_core(tmp_path, [([call("write_file", "c1", path="note.txt", content="hi")], ""),
                                   ([call("finish", "c2", status="done", summary="wrote it")], ""),
                                   ([], "wrote it.")])
    pack = {**TINY, "tools": ["write_file"]}
    try:
        core.packs.install(pack)
        tid = core.submit("", pack="hello-pack", inputs={"who": "Priya"})
        a = pending(core)
        assert a.action == "write_file" and a.tainted is True
        core.bus.decide(a.id, a.nonce, True, by="test")
        assert core.wait_idle()
        assert core.memory.get_task(tid).status == "done"
    finally:
        core.shutdown()


def test_toolbox_allowlist_with_mcp_pattern(tmp_path):
    from types import SimpleNamespace as NS

    from lantern.approvals import CallbackGate
    from lantern.mcp_client import ToolRef
    from lantern.memory import Memory
    from lantern.tools import ToolError, Toolbox
    (tmp_path / "docs").mkdir()
    mcp = NS(tools={"freecad.box": ToolRef("freecad.box", "freecad", "box", "make a box", {}, read_only=True)},
             call=lambda function, args: {"text": "boxed", "is_error": False, "structured": None})
    tb = Toolbox([tmp_path / "docs"], Memory(tmp_path / "m.db"), CallbackGate(lambda s: True), mcp=mcp)
    tb.allowed_tools = {"freecad.*", "list_folders"}
    assert tb.call("freecad.box", {})["server"] == "freecad"
    assert tb.call("list_folders", {})["allowed_folders"]
    with pytest.raises(ToolError, match="may not use write_file"):
        tb.call("write_file", {"path": "x.txt", "content": "hi"})


def test_stop_cancels_a_running_pack_task(tmp_path):
    core, _, _ = make_core(tmp_path, [([call("write_file", "c1", path="note.txt", content="hi")], ""),
                                   ([call("finish", "c2", status="done", summary="wrote it")], ""),
                                   ([], "wrote it.")])
    try:
        core.packs.install({**TINY, "tools": ["write_file"]})
        tid = core.submit("", pack="hello-pack", inputs={"who": "Priya"})
        pending(core)  # the task is blocked on its approval card
        core.stop()
        assert core.wait_idle()
        assert core.memory.get_task(tid).status == "cancelled"
    finally:
        core.shutdown()


def test_disabled_or_unknown_pack_wont_run(tmp_path):
    core, _, _ = make_core(tmp_path, [])
    try:
        core.packs.install(TINY)
        with pytest.raises(ValueError, match="needs a value"):
            core.submit("", pack="hello-pack", inputs={})
        core.packs.disable("hello-pack")
        with pytest.raises(ValueError, match="disabled"):
            core.submit("", pack="hello-pack", inputs={"who": "x"})
        with pytest.raises(KeyError):
            core.submit("", pack="nope", inputs={})
    finally:
        core.shutdown()


# ---- draft --------------------------------------------------------------------
def drafting_task(core):
    tid = core.submit("tidy my downloads")
    assert core.wait_idle()
    return tid


def test_draft_from_finished_task(tmp_path):
    draft = good_pack()
    script = [([call("list_dir", "c1", path="")], ""),
              ([call("finish", "c2", status="done", summary="tidied")], ""),
              ([], "tidied."),
              ([], json.dumps(draft))]
    core, _, fake = make_core(tmp_path, script)
    try:
        tid = drafting_task(core)
        data = core.pack_draft(tid)
        assert validate(data).as_dict() == data
        assert data["format"] == "lantern-agent/1"
        req = fake.requests[-1]
        assert req["model"] == models.SPARK and req["reasoning"] == {"effort": "low"}
        assert req["text"] == {"format": {"type": "json_schema", "name": "agent_pack_draft",
                                          "schema": req["text"]["format"]["schema"], "strict": True}}
        assert core.memory.task_usage(tid)["calls"] >= 2  # the draft's cost is logged to the task
    finally:
        core.shutdown()


def test_draft_from_bad_task_or_bad_json(tmp_path):
    core, _, _ = make_core(tmp_path, [([], "tidied."), ([], "not json")])
    try:
        tid = drafting_task(core)
        with pytest.raises(ValueError, match="no task"):
            core.pack_draft(tid + 99)
        with pytest.raises(ValueError):
            core.pack_draft(tid)
    finally:
        core.shutdown()


# ---- server routes --------------------------------------------------------------
TOKEN = "test-token"
H = {"X-Lantern-Token": TOKEN}
PACK = good_pack()


def make_http(tmp_path, script, approval_timeout=30):
    core, _, fake = make_core(tmp_path, script, approval_timeout)
    return core, TestClient(create_app(core, TOKEN)), fake


def test_agent_routes_need_token(tmp_path):
    core, http, _ = make_http(tmp_path, [])
    try:
        assert http.get("/agents").status_code == 401
        assert http.get("/agents/x").status_code == 401
        assert http.post("/agents/install", json=PACK).status_code == 401
        assert http.post("/agents/x/run", json={"inputs": {}}).status_code == 401
        assert http.post("/agents/x/disable").status_code == 401
        assert http.post("/agents/x/enable").status_code == 401
        assert http.get("/agents/x/export").status_code == 401
        assert http.post("/agents/draft", json={"task_id": 1}).status_code == 401
        assert http.get("/agents", headers=H).json() == {"agents": []}
    finally:
        core.shutdown()


def install_approved(http, pack, query=""):
    """POST /agents/install in a thread (it blocks on the approval card), approve it, return the result."""
    out = {}
    t = threading.Thread(target=lambda: out.update(
        http.post(f"/agents/install{query}", json=pack, headers=H).json()))
    t.start()
    end = time.time() + 10
    cards = []
    while time.time() < end:
        cards = http.get("/approvals", headers=H).json()
        if cards:
            break
        time.sleep(0.01)
    assert cards and cards[0]["action"] == "install_agent"
    for tool in pack["tools"]:
        assert tool in cards[0]["summary"]
    http.post(f"/approvals/{cards[0]['id']}", headers=H,
              json={"nonce": cards[0]["nonce"], "approve": True,
                    "expected_hash": cards[0]["payload_hash"]}).json()
    t.join(timeout=15)
    assert not t.is_alive()
    return out


def test_install_run_export_round_trip(tmp_path):
    script = finish_script("tidied Downloads")
    core, http, _ = make_http(tmp_path, script)
    try:
        out = install_approved(http, PACK)
        assert out == {"installed": True, "verified": True, "name": "tidy-test"}
        assert http.get("/agents", headers=H).json()["agents"][0]["name"] == "tidy-test"
        assert http.get("/agents/tidy-test", headers=H).json()["author"] == "bea"
        out = install_approved(http, {**PACK, "author": "cal"}, "?replace=true")
        assert out == {"installed": True, "verified": True, "name": "tidy-test"}
        assert http.get("/agents/tidy-test", headers=H).json()["author"] == "cal"
        r = http.post("/agents/tidy-test/run", headers=H, json={"inputs": {"folder": "Downloads"}})
        assert core.wait_idle()
        task = http.get(f"/tasks/{r.json()['task_id']}", headers=H).json()
        assert task["goal"] == "[agent:tidy-test] Tidy Downloads into subfolders."
        assert task["status"] == "done"
        export = http.get("/agents/tidy-test/export", headers=H)
        assert export.headers["content-type"] == "application/json"
        assert ".lantern-agent.json" in export.headers["content-disposition"]
        current = {**PACK, "author": "cal"}  # the replace install above is what is stored now
        assert export.content == canonical_bytes(current)  # the same bytes install approved
        assert json.loads(export.content) == current
        assert http.post("/agents/tidy-test/disable", headers=H).json() == {"name": "tidy-test", "disabled": True}
        assert http.post("/agents/tidy-test/run", headers=H,
                         json={"inputs": {"folder": "x"}}).status_code == 400
        assert http.post("/agents/tidy-test/enable", headers=H).json() == {"name": "tidy-test", "disabled": False}
        assert http.get("/agents/nope", headers=H).status_code == 404
        assert http.post("/agents/nope/run", headers=H, json={"inputs": {}}).status_code == 404
        assert http.post("/agents/install", json={"format": "nope"}, headers=H).status_code == 400
    finally:
        core.shutdown()


def test_draft_route(tmp_path):
    draft = good_pack()
    script = [([call("list_dir", "c1", path="")], ""),
              ([call("finish", "c2", status="done", summary="tidied")], ""),
              ([], "tidied."),
              ([], json.dumps(draft))]
    core, http, _ = make_http(tmp_path, script)
    try:
        tid = http.post("/task", json={"goal": "tidy my downloads"}, headers=H).json()["task_id"]
        assert core.wait_idle()
        data = http.post("/agents/draft", json={"task_id": tid}, headers=H).json()
        assert validate(data).name == "tidy-test"
        assert http.post("/agents/draft", json={"task_id": tid + 99}, headers=H).status_code == 404
    finally:
        core.shutdown()
