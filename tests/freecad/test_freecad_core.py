"""S17: operation rules, the feature tree on the fake backend, renders, risk tiers and zones."""
import math
import struct
import zlib

import pytest

from lantern.integrations.freecad import ops, tiers
from lantern.integrations.freecad.fake import FakeBackend
from lantern.integrations.freecad.render import box_triangles, render
from lantern.integrations.freecad.tree import TreeError
from lantern.integrations.freecad.verify import profile_angles
from lantern.policy import PRIVATE, WRITABLE, Policy, Rule


def enclosure(height=15):
    b = FakeBackend()
    for op, a in [("new_document", {"name": "d"}), ("box", dict(name="enclosure", length=40, width=20, height=height)),
                  ("hollow", dict(target="enclosure", thickness=2)),
                  ("hole", dict(target="enclosure", diameter=3.4, x=6, y=10)),
                  ("hole", dict(target="enclosure", diameter=3.4, x=34, y=10))]:
        b.call(op, a)
    return b


# ---- ops: typed, bounded, no code ----------------------------------------------------------
@pytest.mark.parametrize("op,args,msg", [
    ("box", {"name": "1bad", "length": 1, "width": 1, "height": 1}, "start with a letter"),
    ("box", {"name": "b", "length": -1, "width": 1, "height": 1}, "more than 0"),
    ("box", {"name": "b", "length": float("nan"), "width": 1, "height": 1}, "must be a number"),
    ("box", {"name": "b", "length": 5000, "width": 1, "height": 1}, "between"),
    ("box", {"name": "b", "length": True, "width": 1, "height": 1}, "must be a number"),
    ("box", {"name": "b", "length": 1, "width": 1}, "needs height"),
    ("box", {"name": "b", "length": 1, "width": 1, "height": 1, "code": "import os"}, "doesn't take code"),
    ("sketch_extrude", {"name": "s", "points": [[0, 0], [1, 1], [2, 2]], "height": 1}, "don't enclose an area"),
    ("export", {"names": ["a"], "path": "x.stl", "format": "obj"}, "one of stl, step, dxf"),
    ("set_dimension", {"name": "a", "parameter": "transforms", "value": 1}, "must be one of"),
    ("python", {}, "unknown operation"),
])
def test_bad_requests_are_refused(op, args, msg):
    with pytest.raises(ops.OpError, match=msg):
        ops.validate(op, args)


def test_there_is_no_operation_that_runs_code():
    for name in ops.SPEC:
        assert not any(w in name for w in ("exec", "eval", "python", "code", "script", "macro", "shell"))


# ---- tree + fake geometry --------------------------------------------------------------------
@pytest.mark.full_render
def test_enclosure_measures_right():
    m = enclosure().call("measure", {"name": "enclosure"})
    assert (m["bbox"]["x"], m["bbox"]["y"], m["bbox"]["z"]) == (40, 20, 15)
    assert m["solids"] == 1 and m["is_valid"] and m["approx"] and m["backend"] == "fake"
    holes = m["features"]["holes"]
    assert sorted(h["x"] for h in holes) == [6, 34] and all(h["openings"] == 1 for h in holes)
    analytic = 40 * 20 * 15 - 36 * 16 * 13 - 2 * math.pi * 1.7 ** 2 * 2
    assert abs(m["volume"] - analytic) / analytic < 0.08


def test_dimension_edit_follows_through_holes_and_shell():
    b = enclosure()
    r = b.call("set_dimension", {"name": "enclosure", "parameter": "height", "value": 25})
    assert r["old"] == 15 and r["new"] == 25 and r["in"].startswith("enclosure_base")
    m = b.call("measure", {"name": "enclosure"})
    assert m["bbox"]["z"] == 25 and len(m["features"]["holes"]) == 2 and m["features"]["shells"][0]["thickness"] == 2
    with pytest.raises(TreeError, match="no dimension"):
        b.call("set_dimension", {"name": "enclosure", "parameter": "radius", "value": 3})


def test_refusals_are_specific():
    b = enclosure()
    with pytest.raises(TreeError, match="outside enclosure"):
        b.call("hole", {"target": "enclosure", "diameter": 3, "x": 99, "y": 10})
    with pytest.raises(TreeError, match="don't fit"):
        b.call("hollow", {"target": "enclosure", "thickness": 15})
    with pytest.raises(TreeError, match="no object named"):
        b.call("measure", {"name": "ghost"})
    with pytest.raises(TreeError, match="already exists"):
        b.call("box", {"name": "enclosure", "length": 1, "width": 1, "height": 1})
    with pytest.raises(TreeError, match="new_document first"):
        FakeBackend().call("box", {"name": "a", "length": 1, "width": 1, "height": 1})


def test_side_wall_hole_by_boolean_counts_two_openings():
    b = FakeBackend()
    for op, a in [("new_document", {"name": "d"}), ("box", dict(name="box", length=40, width=20, height=15)),
                  ("hollow", dict(target="box", thickness=2)),
                  ("cylinder", dict(name="c", radius=2.5, height=50, x=-5, y=10, z=8, axis="x")),
                  ("boolean", dict(operation="cut", base="box", tool="c", result="box"))]:
        b.call(op, a)
    (hole,) = b.call("measure", {"name": "box"})["features"]["holes"]
    assert hole["axis"] == "x" and hole["openings"] == 2 and hole["diameter"] == 5
    assert [o["name"] for o in b.call("list_objects", {})["objects"]] == ["box"]


def test_fuse_two_parts_is_one_solid_and_apart_parts_are_two():
    b = FakeBackend()
    b.call("new_document", {"name": "d"})
    b.call("box", dict(name="a", length=10, width=10, height=10))
    b.call("box", dict(name="b", length=10, width=10, height=10, x=5))
    b.call("box", dict(name="far", length=5, width=5, height=5, x=50))
    assert b.call("distance", {"a": "a", "b": "b"})["intersecting"] is True
    far = b.call("distance", {"a": "a", "b": "far"})
    assert far["intersecting"] is False and far["distance"] == 40.0   # a ends at x=10, far starts at x=50
    b.call("boolean", dict(operation="fuse", base="a", tool="b", result="ab"))
    m = b.call("measure", {"name": "ab"})
    assert m["solids"] == 1 and m["bbox"]["x"] == 15
    b.call("boolean", dict(operation="fuse", base="ab", tool="far", result="all"))
    assert b.call("measure", {"name": "all"})["solids"] == 2


def test_move_and_rotate_change_the_bounding_box():
    b = FakeBackend()
    b.call("new_document", {"name": "d"})
    b.call("box", dict(name="p", length=30, width=10, height=5))
    b.call("move", {"name": "p", "dz": 10})
    assert b.call("measure", {"name": "p"})["bbox"]["zmin"] == 10
    b.call("rotate", {"name": "p", "axis": "z", "angle": 90})
    bb = b.call("measure", {"name": "p"})["bbox"]
    assert (round(bb["x"], 3), round(bb["y"], 3)) == (10, 30)


def test_phone_stand_profile_has_60_degrees():
    import json
    from pathlib import Path
    tasks = json.loads((Path(__file__).parents[2] / "demo/freecad/tasks.json").read_text(encoding="utf-8"))["tasks"]
    points = next(t for t in tasks if t["id"] == "phone_stand")["plan"][1][1]["points"]
    assert any(abs(a - 60) < 0.2 for a in profile_angles(points))
    assert profile_angles([[0, 0], [10, 0], [10, 10], [0, 10]]) == []


def test_exports_from_the_fake_are_labelled():
    b = enclosure()
    stl = b.call("export", {"names": ["enclosure"], "path": "x.stl", "format": "stl"})["bytes"]
    assert stl.startswith(b"solid enclosure") and b"facet normal" in stl
    step = b.call("export", {"names": ["enclosure"], "path": "x.step", "format": "step"})["bytes"]
    assert step.startswith(b"ISO-10303-21") and b"not a real solid" in step
    dxf = b.call("export", {"names": ["enclosure"], "path": "x.dxf", "format": "dxf"})["bytes"]
    assert b"SECTION" in dxf and dxf.count(b"LINE") == 4


# ---- render ------------------------------------------------------------------------------
def _png_size(png: bytes):
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    w, h = struct.unpack(">II", png[16:24])
    return w, h


def test_render_is_a_valid_three_view_png():
    png = render(box_triangles(0, 0, 0, 40, 20, 15))
    w, h = _png_size(png)
    assert (w, h) == (780, 260)
    idat = png[png.index(b"IDAT") + 4:png.index(b"IEND") - 8]
    raw = zlib.decompress(idat)
    assert len(raw) == h * (1 + 3 * w)
    assert _png_size(render([])) == (780, 260)       # an empty model still renders


@pytest.mark.full_render
def test_fake_render_shows_the_holes_from_above():
    png = enclosure().call("render", {})["png"]
    raw = zlib.decompress(png[png.index(b"IDAT") + 4:png.index(b"IEND") - 8])
    w = 780
    row = 130   # middle row of the top view (third panel)
    pixels = [tuple(raw[row * (1 + 3 * w) + 1 + 3 * x: row * (1 + 3 * w) + 4 + 3 * x]) for x in range(520, 780)]
    background = pixels[2]
    holes = sum(1 for a, b in zip(pixels, pixels[1:]) if a != background and b == background)
    assert holes >= 3   # the part's outline edge plus two hole openings on this row


# ---- tiers ---------------------------------------------------------------------------------
@pytest.fixture
def zones(tmp_path):
    work = tmp_path / "CAD"
    work.mkdir()
    (tmp_path / "Private").mkdir()
    (tmp_path / ".ssh").mkdir()
    policy = Policy([Rule.make(work, WRITABLE), Rule.make(tmp_path / "Private", PRIVATE), Rule.make(tmp_path, "open")])
    return tmp_path, policy


def test_tiers_table():
    assert tiers.TIERS["measure"] == tiers.GREEN and tiers.TIERS["hole"] == tiers.YELLOW
    assert tiers.TIERS["export"] == tiers.RED and set(tiers.GREEN_TOOLS) == {
        "list_objects", "measure", "distance", "render_view", "check_model"}


def test_export_is_red_with_the_exact_file(zones):
    root, policy = zones
    rv = tiers.review("export", {"names": ["enclosure"], "path": str(root / "CAD/e.stl"), "format": "stl"}, policy)
    assert rv.tier == "red" and rv.files == [str((root / "CAD/e.stl").resolve())] and not rv.overwrites
    assert str((root / "CAD/e.stl").resolve()) in rv.summary and "STL" in rv.summary
    (root / "CAD/e.stl").write_text("old", encoding="utf-8")
    rv = tiers.review("export", {"names": ["enclosure"], "path": str(root / "CAD/e.stl"), "format": "stl"}, policy,
                      checks={"passed": False, "failed": ["z: wanted 25"]})
    assert rv.overwrites and "REPLACES" in rv.summary and "NOT passed" in rv.summary


def test_save_is_yellow_new_red_replace(zones):
    root, policy = zones
    assert tiers.review("save", {"path": str(root / "CAD/m.FCStd")}, policy).tier == "yellow"
    (root / "CAD/m.FCStd").write_bytes(b"PK")
    assert tiers.review("save", {"path": str(root / "CAD/m.FCStd")}, policy).tier == "red"


@pytest.mark.parametrize("where,why", [("Private/e.stl", "private zone"), ("e.stl", "open zone"),
                                       (".ssh/e.stl", "outside the folders"), ("CAD/e.exe", "should end in .stl"),
                                       ("CAD/../../e.stl", "outside the folders")])
def test_files_only_in_writable_zones(zones, where, why):
    root, policy = zones
    with pytest.raises(tiers.TierError, match=why):
        tiers.review("export", {"names": ["a"], "path": str(root / where), "format": "stl"}, policy)


def test_payload_is_bound_to_exact_args(zones):
    root, policy = zones
    a = tiers.review("box", {"name": "a", "length": 1, "width": 1, "height": 1}, policy).payload
    b = tiers.review("box", {"name": "a", "length": 2, "width": 1, "height": 1}, policy).payload
    assert a["args_sha256"] != b["args_sha256"]
