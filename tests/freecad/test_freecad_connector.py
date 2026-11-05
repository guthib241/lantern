"""S17: the MCP server through Lantern's real MCP client, tiers + approvals, and the verify loop (FakeClient)."""
import json

import pytest

from fakes import FakeClient
from lantern.integrations.freecad import tiers
from lantern.integrations.freecad.connector import FreeCADConnector, add_to_mcp_json, server_spec
from lantern.integrations.freecad.fake import FakeBackend
from lantern.integrations.freecad.server import Session, build_server, session_from_env
from lantern.mcp_client import MCPHub, with_deferred
from lantern.memory import Memory
from lantern.policy import Policy
from lantern.tools import Toolbox

GOOD_EXPECT = {"object": "enclosure", "size": {"x": 40, "y": 20, "z": 15}, "holes": {"count": 2, "diameter": 3.4},
               "wall": 2, "angle_deg": None, "solids": 1, "notes": ""}


def visual(**verdicts):
    checks = [{"name": n, "verdict": verdicts.get(n, "yes"), "note": f"{n} note"}
              for n in ("dimensions_match", "holes_as_asked", "no_intersections", "valid_single_solid", "matches_goal")]
    bad = [c for c in checks if c["verdict"] == "no"]
    return json.dumps({"checks": checks, "overall": "fail" if bad else "pass", "problem": bad[0]["note"] if bad else ""})


@pytest.fixture
def cad(tmp_path):
    root = tmp_path / "home"
    (root / "CAD").mkdir(parents=True)
    policy = Policy.from_roots([root])
    session = Session(FakeBackend(), policy, tmp_path / "renders")
    hub = MCPHub(servers={"freecad": build_server(session)}).start()
    for ref in hub.tools.values():
        ref.read_only = ref.tool in tiers.GREEN_TOOLS
    mem = Memory(tmp_path / "m.db")
    asked = []
    answers = {"value": True}
    tb = Toolbox(policy, mem, lambda s: asked.append(s) or answers["value"], mcp=hub)
    tb.task_id = mem.start_task("Make an enclosure 40x20x15 mm with 2 mm walls and two M3 holes.").id

    def connector(script=None, **kw):
        return FreeCADConnector(policy, FakeClient(script) if script is not None else None, mem, **kw)
    yield root, hub, tb, mem, asked, answers, connector
    hub.close()


def build(conn, tb, steps):
    out = []
    for fn, a in steps:
        out.append(conn.call(tb, f"freecad.{fn}", a))
    return out


ENCLOSURE = [("new_document", {"name": "d"}), ("box", dict(name="enclosure", length=40, width=20, height=15)),
             ("hollow", dict(target="enclosure", thickness=2)),
             ("hole", dict(target="enclosure", diameter=3.4, x=6, y=10)),
             ("hole", dict(target="enclosure", diameter=3.4, x=34, y=10))]
CHECK = dict(name="enclosure", size_x=40, size_y=20, size_z=15, hole_count=2, hole_diameter=3.4, wall=2)


# ---- registration ------------------------------------------------------------------------------
def test_tools_register_as_deferred_with_tool_search(cad):
    _, hub, *_ = cad
    names = {ref.tool for ref in hub.tools.values()}
    assert {"box", "hole", "hollow", "boolean", "set_dimension", "sketch_extrude", "measure", "export", "check_model"} <= names
    assert not any(w in n for n in names for w in ("exec", "python", "code", "script"))
    specs = hub.function_specs()
    assert all(s["defer_loading"] is True and s["name"].startswith("freecad.") and s["name"].count(".") == 1 for s in specs)
    tools = with_deferred([{"type": "function", "name": "finish"}], specs)
    assert tools[-1] == {"type": "tool_search"} and len(tools) == len(specs) + 2
    assert all("[MCP server 'freecad', untrusted]" in s["description"] for s in specs)
    assert {r.tool for r in hub.tools.values() if r.read_only} == set(tiers.GREEN_TOOLS)


def test_server_spec_and_mcp_json(tmp_path):
    from lantern.config import Config
    cfg = Config(roots=[str(tmp_path / "a"), str(tmp_path / "b")], db_path=str(tmp_path / "m.db"))
    cfg.mcp_config = str(tmp_path / "mcp.json")
    cfg.zones_file = str(tmp_path / "zones.json")
    spec = server_spec(cfg)
    assert spec["args"] == ["-m", "lantern.integrations.freecad.server"] and spec["read_only"] == tiers.GREEN_TOOLS
    assert spec["env"]["LANTERN_FREECAD_BACKEND"] == "auto" and "MODEL_API_KEY" not in json.dumps(spec)
    (tmp_path / "mcp.json").write_text(json.dumps({"servers": {"notes": {"command": "x"}}}), encoding="utf-8")
    data = json.loads(add_to_mcp_json(cfg, backend="fake").read_text(encoding="utf-8"))
    assert set(data["servers"]) == {"notes", "freecad"} and data["servers"]["freecad"]["env"]["LANTERN_FREECAD_BACKEND"] == "fake"


def test_session_from_env_needs_zones(tmp_path):
    with pytest.raises(SystemExit):
        session_from_env({})
    s = session_from_env({"LANTERN_FREECAD_ROOTS": str(tmp_path), "LANTERN_FREECAD_BACKEND": "fake",
                          "LANTERN_FREECAD_RENDERS": str(tmp_path / "r")})
    assert s.backend.name == "fake"


# ---- tiers and approvals -------------------------------------------------------------------------
def test_green_runs_without_asking_yellow_asks_once(cad):
    root, hub, tb, mem, asked, answers, connector = cad
    conn = connector(visual="off")
    build(conn, tb, ENCLOSURE[:2])
    assert len(asked) == 2 and asked[1].startswith("Add a box 'enclosure' 40 × 20 × 15 mm")
    m = conn.call(tb, "freecad.measure", {"name": "enclosure"})
    assert len(asked) == 2 and m["tier"] == "green" and m["bbox"]["z"] == 15


def test_change_returns_numbers_and_a_render(cad):
    root, hub, tb, mem, asked, answers, connector = cad
    r = build(connector(visual="off"), tb, ENCLOSURE)[-1]
    assert r["changed"] == "enclosure" and r["measure"]["bbox"]["x"] == 40 and len(r["measure"]["features"]["holes"]) == 2
    from pathlib import Path
    assert Path(r["render"]).read_bytes().startswith(b"\x89PNG")


def test_export_red_card_lists_the_file_and_writes_it(cad):
    root, hub, tb, mem, asked, answers, connector = cad
    conn = connector(visual="off")
    build(conn, tb, ENCLOSURE)
    path = root / "CAD" / "enclosure.stl"
    r = conn.call(tb, "freecad.export", {"names": ["enclosure"], "path": str(path), "format": "stl"})
    assert r["tier"] == "red" and r["verified"] and path.read_bytes().startswith(b"solid")
    assert str(path.resolve()) in asked[-1] and "never checked" in json.dumps(r["checks_before_export"])
    r = conn.call(tb, "freecad.export", {"names": ["enclosure"], "path": str(path), "format": "stl"})
    assert "REPLACES" in asked[-1] and r["replaced"] is True


def test_declined_export_writes_nothing(cad):
    root, hub, tb, mem, asked, answers, connector = cad
    conn = connector(visual="off")
    build(conn, tb, ENCLOSURE)
    answers["value"] = False
    r = conn.call(tb, "freecad.export", {"names": ["enclosure"], "path": str(root / "CAD/e.step"), "format": "step"})
    assert r == {"done": False, "reason": "user declined", "tier": "red"} and not (root / "CAD/e.step").exists()


def test_paths_outside_writable_zones_never_reach_freecad(cad, tmp_path):
    root, hub, tb, mem, asked, answers, connector = cad
    conn = connector(visual="off")
    build(conn, tb, ENCLOSURE)
    n = len(asked)
    for bad in (tmp_path / "outside.stl", root / ".ssh" / "e.stl", root / "CAD" / "e.txt"):
        r = conn.call(tb, "freecad.export", {"names": ["enclosure"], "path": str(bad), "format": "stl"})
        assert r["tier"] == "refused" and not bad.exists()
    assert len(asked) == n   # refused before any approval card


def test_server_enforces_zones_and_overwrite_even_without_the_connector(cad, tmp_path):
    """Before the merge hook, calls come through the generic MCP path: the server still guards files."""
    root, hub, tb, mem, asked, answers, connector = cad
    build(connector(visual="off"), tb, ENCLOSURE)
    out = hub.call("freecad.export", {"names": ["enclosure"], "path": str(tmp_path / "x.stl"), "format": "stl"})
    assert out["is_error"] and "outside the folders" in out["text"] and not (tmp_path / "x.stl").exists()
    ok = hub.call("freecad.export", {"names": ["enclosure"], "path": str(root / "CAD/y.stl"), "format": "stl"})
    assert not ok["is_error"]
    again = hub.call("freecad.export", {"names": ["enclosure"], "path": str(root / "CAD/y.stl"), "format": "stl"})
    assert again["is_error"] and "overwrite=true" in again["text"]


def test_generic_mcp_path_approves_non_green_and_skips_green(cad):
    root, hub, tb, mem, asked, answers, connector = cad
    tb.call("freecad.new_document", {"name": "d"})
    tb.call("freecad.box", dict(name="p", length=1, width=1, height=1))
    n = len(asked)
    tb.call("freecad.measure", {"name": "p"})
    assert n == 2 and len(asked) == 2


# ---- verify loop ---------------------------------------------------------------------------------
def test_good_model_passes_numbers_and_eyes(cad):
    root, hub, tb, mem, asked, answers, connector = cad
    conn = connector([([], visual())], visual="on_check")
    build(conn, tb, ENCLOSURE)
    r = conn.call(tb, "freecad.check_model", CHECK)
    c = r["check"]
    assert c["passed"] and c["visual"]["overall"] == "pass" and "done" in c["say"]
    req = conn.client.requests[0]
    assert req["text"]["format"]["type"] == "json_schema" and req["text"]["format"]["strict"] is True
    content = req["input"][0]["content"]
    assert content[1]["type"] == "input_image" and content[1]["image_url"].startswith("data:image/png;base64,")
    assert req["model"] == "muse-spark-1.3" and req["reasoning"] == {"effort": "low"}


def test_eyes_can_fail_a_model_the_numbers_pass(cad):
    root, hub, tb, mem, asked, answers, connector = cad
    conn = connector([([], visual(holes_as_asked="no"))], visual="on_check")
    build(conn, tb, ENCLOSURE)
    c = conn.call(tb, "freecad.check_model", CHECK)["check"]
    assert not c["passed"] and any("holes_as_asked" in f for f in c["failed"]) and all(n["passed"] for n in c["numeric"])


def test_unsure_eyes_never_turn_a_failure_into_a_pass(cad):
    root, hub, tb, mem, asked, answers, connector = cad
    conn = connector([([], visual(dimensions_match="unsure"))], visual="on_check")
    build(conn, tb, ENCLOSURE)
    c = conn.call(tb, "freecad.check_model", {**CHECK, "size_z": 30})["check"]
    assert not c["passed"] and "z: wanted 30 mm" in " ".join(c["failed"]) and c["notes"]


def test_one_fix_attempt_then_honest(cad):
    root, hub, tb, mem, asked, answers, connector = cad
    conn = connector([([], visual(dimensions_match="no"))] * 2 + [([], visual())], visual="on_check")
    build(conn, tb, ENCLOSURE)
    first = conn.call(tb, "freecad.check_model", {**CHECK, "size_z": 20})["check"]
    assert not first["passed"] and "1 fix attempt" in first["say"] and not first.get("give_up")
    conn.call(tb, "freecad.set_dimension", {"name": "enclosure", "parameter": "height", "value": 18})
    second = conn.call(tb, "freecad.check_model", {**CHECK, "size_z": 20})["check"]
    assert not second["passed"] and second["give_up"] and "Do not say it is done" in second["say"]
    nxt = conn.call(tb, "freecad.export", {"names": ["enclosure"], "path": str(root / "CAD/e.stl"), "format": "stl"})
    assert "NOT passed" in asked[-1] and nxt["checks_before_export"]["passed"] is False


def test_expectations_come_from_the_goal_and_every_change_is_checked(cad):
    root, hub, tb, mem, asked, answers, connector = cad
    script = [([], json.dumps(GOOD_EXPECT))] + [([], visual())] * 4
    conn = connector(script, visual="every_change")
    results = build(conn, tb, ENCLOSURE)
    state = conn.tasks[tb.task_id]
    assert state.expect == GOOD_EXPECT and "Make an enclosure" in conn.client.requests[0]["input"][0]["content"][0]["text"]
    assert results[1]["verify"]["passed"] is False and results[-1]["verify"]["passed"] is True
    assert [r["verify"]["stage"] for r in results[1:]] == ["after change"] * 4
    usage = mem.task_usage(tb.task_id)
    assert usage["calls"] == 5


def test_spark_down_means_numbers_decide_and_say_so(cad):
    root, hub, tb, mem, asked, answers, connector = cad

    class Down:
        responses = None

        def __init__(self):
            self.responses = self

        def create(self, **kw):
            raise ConnectionError("offline")
    conn = FreeCADConnector(tb.policy, Down(), mem, visual="on_check")
    conn.tasks.setdefault(tb.task_id, conn._state(tb)).expect = GOOD_EXPECT
    build(conn, tb, ENCLOSURE)
    r = conn.call(tb, "freecad.check_model", CHECK)
    assert r["check"]["passed"] and "no visual check" in " ".join(r["check"]["notes"])


def test_bad_tool_args_come_back_as_errors(cad):
    root, hub, tb, mem, asked, answers, connector = cad
    conn = connector(visual="off")
    build(conn, tb, ENCLOSURE[:2])
    r = conn.call(tb, "freecad.hole", dict(target="enclosure", diameter=3, x=500, y=10))
    assert "error" in r
    r = conn.call(tb, "freecad.box", dict(name="2bad", length=1, width=1, height=1))
    assert "error" in r


def test_format_in_capitals_works_and_unknown_format_is_a_clear_error(cad):
    """Live run, Oct 27: Muse Spark asked for format "STL" and the task crashed with KeyError."""
    root, hub, tb, mem, asked, answers, connector = cad
    conn = connector(visual="off")
    build(conn, tb, ENCLOSURE)
    r = conn.call(tb, "freecad.export", {"names": ["enclosure"], "path": str(root / "CAD/e.stl"), "format": "STL"})
    assert r["verified"] and r["format"] == "stl"
    r = conn.call(tb, "freecad.export", {"names": ["enclosure"], "path": str(root / "CAD/e.obj"), "format": "OBJ"})
    assert r["tier"] == "refused" and "stl, step or dxf" in r["error"]


def test_unexpected_errors_come_back_as_tool_errors(cad, monkeypatch):
    root, hub, tb, mem, asked, answers, connector = cad
    conn = connector(visual="off")
    build(conn, tb, ENCLOSURE[:1])

    def boom(*a, **k):
        raise KeyError("surprise")
    monkeypatch.setattr(hub, "call", boom)
    r = conn.call(tb, "freecad.measure", {"name": "x"})
    assert r["tier"] == "error" and "KeyError" in r["error"]
