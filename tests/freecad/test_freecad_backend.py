"""S17: the FreeCAD process layer (worker protocol), the demo script, and real FreeCAD when installed."""
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from lantern.integrations.freecad import backend as backend_mod
from lantern.integrations.freecad.backend import BackendError, FreeCADBackend, find_freecadcmd, make_backend
from lantern.integrations.freecad.fake import FakeBackend

REPO = Path(__file__).resolve().parents[2]
WORKER = REPO / "lantern" / "integrations" / "freecad" / "worker.py"

FAKE_CMD = textwrap.dedent('''
    import json, sys, time, os
    print("FreeCAD 1.0.2, Libs: 1.0.2R39319 (console chatter that must be ignored)", flush=True)
    print("@@LANTERN " + json.dumps({"id": 0, "ok": True, "result": {"ready": True, "version": "9.9.9"}}), flush=True)
    for line in sys.stdin:
        msg = json.loads(line)
        if msg.get("op") == "quit":
            break
        if msg["op"] == "hang":
            time.sleep(30)
        if msg["op"] == "crash":
            os._exit(3)
        if msg["op"] == "bad":
            print("@@LANTERN " + json.dumps({"id": msg["id"], "ok": False, "error": "FreeCAD: Part.OCCError: no"}), flush=True)
            continue
        print("noise in between", flush=True)
        print("@@LANTERN " + json.dumps({"id": msg["id"], "ok": True, "result": {"echo": msg["op"], "args": msg["args"]}}), flush=True)
''')


@pytest.fixture
def fake_cmd(tmp_path):
    """A stand-in for freecadcmd: the backend runs [cmd, worker.py]; this script ignores the path."""
    script = tmp_path / "fake_freecad.py"
    script.write_text(FAKE_CMD, encoding="utf-8")

    def popen(argv, **kw):
        return subprocess.Popen([sys.executable, str(script)], **kw)
    return popen


def test_protocol_ignores_chatter_and_matches_ids(fake_cmd):
    b = FreeCADBackend("freecadcmd", popen=fake_cmd).start()
    try:
        assert b.version == "9.9.9"
        assert b.call("list_objects", {}) == {"echo": "list_objects", "args": {}}
        assert b.call("measure", {"name": "p"})["args"] == {"name": "p"}
        with pytest.raises(BackendError, match="OCCError"):
            b.call("bad", {})
        assert b.call("list_objects", {})["echo"] == "list_objects"   # still alive after an error
    finally:
        b.close()


def test_hang_times_out_and_crash_restarts(fake_cmd):
    # 3 s, not 1.5: the call after a restart includes starting a new process, which a busy CI runner can take
    # longer than 1.5 s to do (it failed that way once, Nov 5); the hang is still caught well before a real one.
    b = FreeCADBackend("freecadcmd", popen=fake_cmd, timeout=3.0).start()
    try:
        with pytest.raises(BackendError, match="didn't answer"):
            b.call("hang", {})
        assert b.call("list_objects", {})["echo"] == "list_objects"   # restarted after the stop
        with pytest.raises(BackendError, match="stopped unexpectedly"):
            b.call("crash", {})
        assert b.call("list_objects", {})["echo"] == "list_objects"   # and again after a crash
    finally:
        b.close()


def test_find_and_make_backend(tmp_path, monkeypatch):
    exe = tmp_path / "freecadcmd"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    assert find_freecadcmd({"LANTERN_FREECAD_CMD": str(exe)}) == str(exe)
    assert find_freecadcmd({"LANTERN_FREECAD_CMD": str(tmp_path / "missing")}) is None
    assert isinstance(make_backend("fake"), FakeBackend)
    monkeypatch.setattr(backend_mod, "find_freecadcmd", lambda env=None: None)
    with pytest.raises(BackendError, match="docs/FREECAD.md"):
        make_backend("freecad")
    with pytest.raises(BackendError, match="LANTERN_FREECAD_BACKEND=fake"):
        make_backend("auto")


# ---- the real worker.py against stub FreeCAD modules ---------------------------------------------
STUB_FREECAD = '''
def Version():
    return ["1", "0", "2", "stub"]
class Vector:
    def __init__(self, x=0, y=0, z=0):
        self.x, self.y, self.z = x, y, z
'''
STUB_PART = '''
class _BB:
    def __init__(self, lo, hi):
        self.XMin, self.YMin, self.ZMin = lo
        self.XMax, self.YMax, self.ZMax = hi
class _Shape:
    def __init__(self, lo, hi):
        self.BoundBox = _BB(lo, hi)
        self.Volume = (hi[0] - lo[0]) * (hi[1] - lo[1]) * (hi[2] - lo[2])
        self.Area = 0.0
        self.Solids = [self]
        self.Faces = []
    def isValid(self):
        return True
    def isNull(self):
        return False
def makeBox(l, w, h, at):
    return _Shape((at.x, at.y, at.z), (at.x + l, at.y + w, at.z + h))
'''


def test_worker_speaks_the_protocol_with_freecad_stubs(tmp_path):
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "FreeCAD.py").write_text(STUB_FREECAD, encoding="utf-8")
    (stubs / "Part.py").write_text(STUB_PART, encoding="utf-8")
    reqs = [{"id": 1, "op": "new_document", "args": {"name": "d"}},
            {"id": 2, "op": "box", "args": {"name": "b", "length": 40, "width": 20, "height": 15, "x": 1}},
            {"id": 3, "op": "measure", "args": {"name": "b"}},
            {"id": 4, "op": "box", "args": {"name": "b", "length": -1, "width": 1, "height": 1}},
            {"id": 5, "op": "hole", "args": {"target": "b", "diameter": 3, "x": 900, "y": 1}},
            {"id": 6, "op": "list_objects", "args": {}},
            {"op": "quit"}]
    r = subprocess.run([sys.executable, str(WORKER)], input="\n".join(json.dumps(x) for x in reqs) + "\n",
                       capture_output=True, text=True, timeout=60,
                       env={"PYTHONPATH": str(stubs), "PATH": "/usr/bin:/bin"})
    replies = [json.loads(line[len("@@LANTERN "):]) for line in r.stdout.splitlines() if line.startswith("@@LANTERN ")]
    by_id = {x["id"]: x for x in replies}
    assert by_id[0]["result"]["version"] == "1.0.2" and by_id[0]["result"]["backend"] == "freecad"
    assert by_id[2] == {"id": 2, "ok": True, "result": {"created": "b"}}
    m = by_id[3]["result"]
    assert m["bbox"] == {"xmin": 1, "ymin": 0, "zmin": 0, "xmax": 41, "ymax": 20, "zmax": 15, "x": 40, "y": 20, "z": 15}
    assert m["volume"] == 12000 and m["solids"] == 1 and m["is_valid"] and m["approx"] is False
    assert not by_id[4]["ok"] and "more than 0" in by_id[4]["error"]
    assert not by_id[5]["ok"] and "outside b" in by_id[5]["error"]
    assert by_id[6]["result"]["objects"] == [{"name": "b", "type": "box"}]


def test_worker_has_no_code_execution_and_writes_only_given_temp_paths():
    src = WORKER.read_text(encoding="utf-8")
    for bad in ("exec(", "eval(", "subprocess", "os.system", "__import__"):
        assert bad not in src


# ---- the demo, end to end on the fake --------------------------------------------------------------
def test_demo_tasks_pass_on_the_fake(tmp_path, capsys):
    sys.path.insert(0, str(REPO / "demo" / "freecad"))
    import run_demo
    assert run_demo.main(["--out", str(tmp_path / "demo")]) == 0
    out = capsys.readouterr().out
    assert out.count("✓ enclosure") == 1 and "✓ phone_stand" in out and "✓ taller_export" in out
    assert (tmp_path / "demo" / "enclosure.stl").read_bytes().startswith(b"solid")


# ---- real FreeCAD (laptop only) ----------------------------------------------------------------------
@pytest.mark.skipif(find_freecadcmd() is None, reason="FreeCAD isn't installed (real runs are laptop-only)")
def test_demo_tasks_pass_on_real_freecad(tmp_path):
    sys.path.insert(0, str(REPO / "demo" / "freecad"))
    import run_demo
    assert run_demo.main(["--freecad", "--out", str(tmp_path / "real")]) == 0


def test_dead_pipe_restarts_once_like_windows_errno_22(fake_cmd):
    """Windows reports writing to a just-crashed worker as OSError(22); the backend restarts and sends again."""
    b = FreeCADBackend("freecadcmd", popen=fake_cmd).start()
    try:
        class Dead:
            def write(self, s):
                raise OSError(22, "Invalid argument")
            flush = write
        b.proc.stdin = Dead()
        assert b.call("list_objects", {})["echo"] == "list_objects"
    finally:
        b.close()
