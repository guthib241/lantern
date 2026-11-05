"""S17: the documented merge hook (docs/FREECAD.md) applied with monkeypatch, so the whole agent path is
tested on this branch without editing lantern/tools.py: Muse Spark (FakeClient) -> deferred freecad.* tools ->
connector tiers and approvals -> MCP server -> fake FreeCAD -> verify loop -> honest finish."""
import json

import pytest

from types import SimpleNamespace as NS

from fakes import FakeClient, call
from lantern.agent import Agent
from lantern.integrations.freecad import tiers
from lantern.integrations.freecad.connector import FreeCADConnector
from lantern.integrations.freecad.fake import FakeBackend
from lantern.integrations.freecad.server import Session, build_server
from lantern.mcp_client import MCPHub
from lantern.memory import Memory
from lantern.policy import Policy
from lantern.tools import Toolbox

def fc(fn, cid, **args):
    """A function call whose arguments may include `name` (fakes.call reserves that word)."""
    return NS(type="function_call", name=fn, call_id=cid, arguments=json.dumps(args))


VISUAL_PASS = json.dumps({"checks": [{"name": n, "verdict": "yes", "note": "ok"} for n in
                                     ("dimensions_match", "holes_as_asked", "no_intersections", "valid_single_solid",
                                      "matches_goal")], "overall": "pass", "problem": ""})


@pytest.fixture
def hooked(tmp_path, monkeypatch):
    original = Toolbox.mcp_call

    def mcp_call(self, function, args):   # exactly merge hook 1 in docs/FREECAD.md
        if getattr(self, "freecad", None) is not None and self.mcp.tools[function].server == "freecad":
            return self.freecad.call(self, function, args)
        return original(self, function, args)
    monkeypatch.setattr(Toolbox, "mcp_call", mcp_call)
    root = tmp_path / "home"
    (root / "CAD").mkdir(parents=True)
    policy = Policy.from_roots([root])
    hub = MCPHub(servers={"freecad": build_server(Session(FakeBackend(), policy, tmp_path / "r"))}).start()
    for ref in hub.tools.values():
        ref.read_only = ref.tool in tiers.GREEN_TOOLS
    mem = Memory(tmp_path / "m.db")
    asked = []
    tb = Toolbox(policy, mem, lambda s: asked.append(s) or True, mcp=hub)
    yield root, hub, tb, mem, asked, policy
    hub.close()


def test_agent_builds_checks_and_exports_through_the_hook(hooked):
    root, hub, tb, mem, asked, policy = hooked
    stl = str(root / "CAD" / "block.stl")
    brain = FakeClient([
        ([fc("freecad.new_document", "c1", name="d")], ""),
        ([fc("freecad.box", "c2", name="block", length=30, width=30, height=10)], ""),
        ([fc("freecad.check_model", "c3", name="block", size_x=30, size_y=30, size_z=10)], ""),
        ([fc("freecad.export", "c4", names=["block"], path=stl, format="stl")], ""),
        ([call("finish", "c5", status="done", summary="30x30x10 block, checked, exported")], ""),
        ([], "Done: 30 × 30 × 10 mm, checks passed, STL saved."),
    ])
    eyes = FakeClient([([], VISUAL_PASS)])
    tb.freecad = FreeCADConnector(policy, eyes, mem, visual="on_check")   # merge hook 2 (Core) in the doc
    agent = Agent(brain, mem, tb)
    r = agent.run("Make a 30x30x10 mm block and export an STL.")
    assert r.status == "done" and (root / "CAD" / "block.stl").read_bytes().startswith(b"solid")
    results = {c["name"]: c["result"] for c in r.tool_calls}
    assert results["freecad.box"]["tier"] == "yellow" and results["freecad.export"]["tier"] == "red"
    assert results["freecad.check_model"]["check"]["passed"] is True
    assert asked[0].startswith("Start a new FreeCAD document") and stl in asked[-1] and "Checks: passed" in asked[-1]
    first = brain.requests[0]["tools"]
    deferred = [t for t in first if t.get("defer_loading")]
    assert deferred and all(t["name"].startswith("freecad.") for t in deferred) and first[-1] == {"type": "tool_search"}


def test_agent_is_told_to_stop_and_be_honest_after_the_fix_attempt(hooked):
    root, hub, tb, mem, asked, policy = hooked
    brain = FakeClient([
        ([fc("freecad.new_document", "c1", name="d")], ""),
        ([fc("freecad.box", "c2", name="block", length=30, width=30, height=12)], ""),
        ([fc("freecad.check_model", "c3", name="block", size_z=10)], ""),
        ([fc("freecad.set_dimension", "c4", name="block", parameter="height", value=11)], ""),
        ([fc("freecad.check_model", "c5", name="block", size_z=10)], ""),
        ([call("finish", "c6", status="failed", summary="height still 11 mm, not 10")], ""),
        ([], "I couldn't get it to 10 mm: it's 11 mm tall."),
    ])
    tb.freecad = FreeCADConnector(policy, None, mem, visual="off")
    r = Agent(brain, mem, tb).run("Make a 30x30x10 mm block.")
    checks = [c["result"]["check"] for c in r.tool_calls if c["name"] == "freecad.check_model"]
    assert [c["passed"] for c in checks] == [False, False] and checks[1]["give_up"]
    fed_back = json.loads(brain.requests[5]["input"][0]["output"])
    assert "Do not say it is done" in fed_back["check"]["say"] and r.status == "failed"


def test_core_and_each_scheduler_task_get_their_own_connector(tmp_path):
    """The hooks as merged into core.py and scheduler.py: Core's toolbox has the connector, and every scheduler
    task gets one with that task's own (audited, budgeted) client."""
    from lantern.core import Core
    from lantern.scheduler import Run
    from test_scheduler import config
    cfg = config(tmp_path)
    policy = Policy.from_roots([tmp_path])
    hub = MCPHub(servers={"freecad": build_server(Session(FakeBackend(), policy, tmp_path / "r"))}).start()
    core = Core(cfg, FakeClient([]), policy=policy, mcp=hub, reminder_tick=0, workers=1)
    try:
        assert isinstance(core.toolbox.freecad, FreeCADConnector)
        agent = core.scheduler._agent(Run(1, "make a 30 mm cube"))
        cad = agent.toolbox.freecad
        assert isinstance(cad, FreeCADConnector) and cad is not core.toolbox.freecad
        assert cad.client is agent.toolbox.client and cad.visual == core.toolbox.freecad.visual
    finally:
        core.shutdown()
        hub.close()
    (tmp_path / "b").mkdir()
    plain = Core(config(tmp_path / "b"), FakeClient([]), reminder_tick=0)
    assert plain.toolbox.freecad is None
    plain.shutdown()
