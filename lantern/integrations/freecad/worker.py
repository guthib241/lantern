"""Runs INSIDE FreeCAD's own Python (FreeCADCmd / freecadcmd), started by backend.FreeCADBackend.

    FreeCADCmd lantern/integrations/freecad/worker.py

Protocol: one JSON request per line on stdin {"id", "op", "args"}; one reply per line on stdout,
prefixed with "@@LANTERN " so FreeCAD's own console chatter is ignored:
    @@LANTERN {"id": 1, "ok": true, "result": {...}}
    @@LANTERN {"id": 1, "ok": false, "error": "..."}
The document is the same parametric feature tree as the fake backend (tree.py); this file only
evaluates it with FreeCAD's Part kernel. It never chooses file paths: save/export write to the
temporary path the server gives it, and the server moves the file into a writable zone through
Lantern's policy. There is no operation that runs arbitrary code.
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))   # the repo root, so tree.py can be imported

from lantern.integrations.freecad import ops  # noqa: E402
from lantern.integrations.freecad.tree import FeatureTree, TreeError  # noqa: E402

PREFIX = "@@LANTERN "
TESSELLATION = 0.1   # mm, for STL and renders


def _fc():
    import FreeCAD  # noqa: F401  (only importable inside FreeCAD)
    import Part
    return FreeCAD, Part


class FreeCADTree(FeatureTree):
    name = "freecad"

    def __init__(self):
        super().__init__()
        self.App, self.Part = _fc()

    # ---- evaluation ----------------------------------------------------------------------
    def shape(self, name: str):
        App, Part = self.App, self.Part
        f = self.objects[name]
        t = f["type"]
        V = App.Vector
        if t == "box":
            s = Part.makeBox(f["length"], f["width"], f["height"], V(f["x"], f["y"], f["z"]))
        elif t == "cylinder":
            d = {"z": V(0, 0, 1), "x": V(1, 0, 0), "y": V(0, 1, 0)}[f["axis"]]
            s = Part.makeCylinder(f["radius"], f["height"], V(f["x"], f["y"], f["z"]), d)
        elif t == "extrude":
            x, y, z = f["x"], f["y"], f["z"]
            if f["plane"] == "XY":
                pts, d = [V(x + u, y + v, z) for u, v in f["points"]], V(0, 0, f["height"])
            elif f["plane"] == "XZ":
                pts, d = [V(x + u, y, z + v) for u, v in f["points"]], V(0, f["height"], 0)
            else:
                pts, d = [V(x, y + u, z + v) for u, v in f["points"]], V(f["height"], 0, 0)
            s = Part.Face(Part.makePolygon(pts + [pts[0]])).extrude(d)
        elif t == "hole":
            base, r = self.shape(f["base"]), f["diameter"] / 2
            if f["depth"] <= 0:
                start, length = f["bottom"] - 1, f["top"] - f["bottom"] + 2
            else:
                start, length = f["top"] - f["depth"], f["depth"] + 1
            s = base.cut(Part.makeCylinder(r, length, V(f["x"], f["y"], start), V(0, 0, 1)))
        elif t == "hollow":
            base = self.shape(f["base"])
            if f["open_face"] == "none":
                s = base.cut(base.makeOffsetShape(-f["thickness"], 1e-3))
            else:
                pick = max if f["open_face"] == "top" else min
                face = pick(base.Faces, key=lambda fc: fc.CenterOfMass.z)
                s = base.makeThickness([face], -f["thickness"], 1e-3)
        elif t in ("fillet", "chamfer"):
            base = self.shape(f["base"])
            edges = self._edges(base, f["edges"])
            if not edges:
                raise TreeError(f"no {f['edges']} edges to {t}")
            s = base.makeFillet(f["radius"], edges) if t == "fillet" else base.makeChamfer(f["size"], edges)
        elif t == "cut":
            s = self.shape(f["base"]).cut(self.shape(f["tool"]))
        elif t == "fuse":
            s = self.shape(f["base"]).fuse(self.shape(f["tool"])).removeSplitter()
        elif t == "common":
            s = self.shape(f["base"]).common(self.shape(f["tool"]))
        else:
            raise TreeError(f"unknown feature type {t}")
        if f.get("transforms"):
            s = s.copy()
            for tr in f["transforms"]:
                if "move" in tr:
                    s.translate(V(*tr["move"]))
                else:
                    axis, angle, c = tr["rotate"]
                    s.rotate(V(*c), {"x": V(1, 0, 0), "y": V(0, 1, 0), "z": V(0, 0, 1)}[axis], angle)
        return s

    @staticmethod
    def _edges(shape, which: str) -> list:
        if which == "all":
            return list(shape.Edges)
        bb, out = shape.BoundBox, []
        for e in shape.Edges:
            zs = [v.Point.z for v in e.Vertexes]
            if len(zs) < 2:
                continue
            xy_same = all(abs(v.Point.x - e.Vertexes[0].Point.x) < 1e-6 and abs(v.Point.y - e.Vertexes[0].Point.y) < 1e-6
                          for v in e.Vertexes)
            if which == "vertical" and xy_same and abs(zs[0] - zs[-1]) > 1e-6:
                out.append(e)
            elif which == "top" and all(abs(z - bb.ZMax) < 1e-6 for z in zs):
                out.append(e)
            elif which == "bottom" and all(abs(z - bb.ZMin) < 1e-6 for z in zs):
                out.append(e)
        return out

    def _feature_bbox(self, name: str):
        bb = self.shape(name).BoundBox
        return (bb.XMin, bb.YMin, bb.ZMin), (bb.XMax, bb.YMax, bb.ZMax)

    # ---- measuring ----------------------------------------------------------------------
    def op_measure(self, name) -> dict:
        self._need(name)
        s = self.shape(name)
        bb = s.BoundBox
        feats = self.features(name)
        for h in feats["holes"]:
            h["openings"] = self._openings(s, h)
        radii = sorted({round(fc.Surface.Radius * 2, 2) for fc in s.Faces
                        if type(fc.Surface).__name__ == "Cylinder"})
        return {"name": name, "backend": self.name, "approx": False,
                "bbox": ops.bbox_dict((bb.XMin, bb.YMin, bb.ZMin), (bb.XMax, bb.YMax, bb.ZMax)),
                "volume": round(s.Volume, 3), "area": round(s.Area, 3), "solids": len(s.Solids),
                "is_valid": bool(s.isValid()) and not s.isNull(), "features": feats,
                "cylinder_diameters": radii}

    def _openings(self, shape, hole: dict) -> int:
        """Walls the hole passes through: solid segments on a line just beside its axis."""
        V, bb = self.App.Vector, shape.BoundBox
        ax, r = hole.get("axis", "z"), hole["diameter"] / 2 + 0.3
        p = [hole["x"], hole["y"], hole.get("z", 0.0)]
        p[0 if ax != "x" else 1] += r
        lo, hi = list(p), list(p)
        i = {"x": 0, "y": 1, "z": 2}[ax]
        lo[i], hi[i] = (bb.XMin, bb.YMin, bb.ZMin)[i] - 1, (bb.XMax, bb.YMax, bb.ZMax)[i] + 1
        return len(shape.common(self.Part.LineSegment(V(*lo), V(*hi)).toShape()).Edges)

    def op_distance(self, a, b) -> dict:
        self._need(a), self._need(b)
        sa, sb = self.shape(a), self.shape(b)
        common = sa.common(sb).Volume
        return {"a": a, "b": b, "distance": round(sa.distToShape(sb)[0], 4), "intersecting": common > 1e-6,
                "common_volume": round(common, 3), "backend": self.name, "approx": False}

    def _triangles(self, names):
        out = []
        for n in names:
            pts, tris = self.shape(n).tessellate(TESSELLATION)
            out += [tuple((p.x, p.y, p.z) for p in (pts[i], pts[j], pts[k])) for i, j, k in tris]
        return out

    def op_mesh(self, names=None) -> dict:
        names = names or list(self.visible)
        for n in names:
            self._need(n)
        return {"triangles": self._triangles(names), "backend": self.name}

    def op_render(self, names=None) -> dict:
        raise TreeError("render runs in the server (it asks for mesh)")

    # ---- files (to the temporary path the server chose) -----------------------------------
    def op_save(self, path) -> dict:
        App = self.App
        doc = App.newDocument("LanternExport")
        try:
            for n in self.visible:
                obj = doc.addObject("Part::Feature", n)
                obj.Shape = self.shape(n)
            doc.recompute()
            doc.saveAs(path)
        finally:
            App.closeDocument(doc.Name)
        return {"written": path, "format": "fcstd"}

    def op_export(self, names, path, format) -> dict:
        for n in names:
            self._need(n)
        shapes = [self.shape(n) for n in names]
        compound = self.Part.makeCompound(shapes)
        if format == "stl":
            import Mesh
            Mesh.Mesh(compound.tessellate(TESSELLATION)).write(path)
        elif format == "step":
            compound.exportStep(path)
        else:   # DXF: the top view's visible edges, flattened, as LINE entities
            import TechDraw
            projected = TechDraw.projectEx(compound, self.App.Vector(0, 0, 1))
            lines = []
            for comp in projected[:1]:   # visible hard edges
                for e in comp.Edges:
                    pts = e.discretize(Deflection=0.05)
                    lines += [((a.x, a.y), (b.x, b.y)) for a, b in zip(pts, pts[1:])]
            Path(path).write_bytes(ops.dxf_lines(lines))
        return {"written": path, "format": format}


def main() -> None:
    tree = FreeCADTree()
    print(PREFIX + json.dumps({"id": 0, "ok": True, "result": {"ready": True, "backend": "freecad",
                                                                 "version": ".".join(tree.App.Version()[:3])}}),
          flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("op") == "quit":
            break
        try:
            result = tree.call(msg["op"], msg.get("args") or {})
            reply = {"id": msg.get("id"), "ok": True, "result": result}
        except (TreeError, ops.OpError) as e:
            reply = {"id": msg.get("id"), "ok": False, "error": str(e)}
        except Exception as e:   # a FreeCAD kernel error: report it, keep the worker alive
            reply = {"id": msg.get("id"), "ok": False, "error": f"FreeCAD: {type(e).__name__}: {e}",
                     "trace": traceback.format_exc()[-800:]}
        print(PREFIX + json.dumps(reply, default=str), flush=True)


if __name__ == "__main__" or ("FreeCAD" in sys.modules and __name__ != "lantern.integrations.freecad.worker"):
    main()   # FreeCADCmd may run the file under another __name__; importing it (tests) never starts the loop
