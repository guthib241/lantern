"""S17 on the laptop: the merge hooks are in place, FreeCAD is found where Windows installs it, doctor checks it."""
import json
import sys
from pathlib import Path

from lantern import doctor
from lantern.agent import BASE_RULES
from lantern.integrations.freecad import backend

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_doctor import READY, cfg_for, env  # noqa: E402


def _install(base: Path, folder: str, exe: str) -> Path:
    p = base / folder / "bin" / exe
    p.parent.mkdir(parents=True)
    p.write_text("")
    return p


def test_finds_freecad_in_program_files_and_per_user_installs(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "CANDIDATES", [])
    pf, local = tmp_path / "Program Files", tmp_path / "AppData" / "Local"
    _install(pf, "FreeCAD 0.21", "FreeCADCmd.exe")
    newest = _install(pf, "FreeCAD 1.2", "freecadcmd.exe")
    (pf / "Other").mkdir()
    assert backend.find_freecadcmd({"ProgramFiles": str(pf)}) == str(newest)
    user = _install(local / "Programs", "FreeCAD 1.0", "freecadcmd.exe")
    assert backend.find_freecadcmd({"LOCALAPPDATA": str(local)}) == str(user)
    assert backend.find_freecadcmd({"ProgramFiles": str(tmp_path / "missing")}) is None


def test_explicit_path_wins_and_a_wrong_one_is_reported(tmp_path):
    exe = _install(tmp_path, "FreeCAD 1.0", "freecadcmd.exe")
    assert backend.find_freecadcmd({"LANTERN_FREECAD_CMD": str(exe)}) == str(exe)
    assert backend.find_freecadcmd({"LANTERN_FREECAD_CMD": str(tmp_path / "nope.exe")}) is None


def test_doctor_checks_freecad_only_when_mcp_json_has_it(tmp_path):
    cfg = cfg_for(tmp_path)
    cfg.mcp_config = str(tmp_path / "mcp.json")
    e, _ = env(READY)
    assert not [c for c in doctor.run_checks(cfg, e) if c.id == "freecad"]
    exe = _install(tmp_path, "FreeCAD 1.0", "freecadcmd.exe")
    spec = {"servers": {"freecad": {"command": "python", "env": {"LANTERN_FREECAD_CMD": str(exe)}}}}
    Path(cfg.mcp_config).write_text(json.dumps(spec))
    c = [c for c in doctor.run_checks(cfg, e) if c.id == "freecad"][0]
    assert c.status == doctor.OK and c.detail == str(exe)
    spec["servers"]["freecad"]["env"]["LANTERN_FREECAD_CMD"] = str(tmp_path / "gone.exe")
    Path(cfg.mcp_config).write_text(json.dumps(spec))
    c = [c for c in doctor.run_checks(cfg, e) if c.id == "freecad"][0]
    assert c.status == doctor.FAIL and "LANTERN_FREECAD_CMD" in c.fix


def test_core_wires_the_connector_when_mcp_has_freecad(tmp_path):
    from fakes import FakeClient
    from lantern.core import Core
    from lantern.integrations.freecad.connector import FreeCADConnector
    from lantern.integrations.freecad.fake import FakeBackend
    from lantern.integrations.freecad.server import Session, build_server
    from lantern.mcp_client import MCPHub
    from lantern.policy import Policy
    cfg = cfg_for(tmp_path)
    hub = MCPHub(servers={"freecad": build_server(Session(FakeBackend(), Policy.from_roots(cfg.roots), tmp_path / "r"))})
    core = Core(cfg, FakeClient([]), reminder_tick=0, mcp=hub.start())
    try:
        assert isinstance(core.toolbox.freecad, FreeCADConnector) and core.toolbox.freecad.client is core.client
    finally:
        core.shutdown()
    plain = Core(cfg_for(tmp_path / "b"), FakeClient([]), reminder_tick=0, mcp=MCPHub())
    try:
        assert plain.toolbox.freecad is None
    finally:
        plain.shutdown()
    assert "freecad.check_model" in BASE_RULES
    assert "FreeCAD" in (Path(__file__).resolve().parents[2] / "docs" / "THIRD_PARTY.md").read_text(encoding="utf-8")
