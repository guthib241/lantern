"""The CAD document as a parametric feature tree, shared by the fake backend and the FreeCAD worker.

Every operation Lantern can ask for (ops.SPEC) becomes a feature: primitives (box, cylinder,
sketch extrusion), modifications (hole, hollow, fillet, chamfer) that keep the old feature as a
hidden base, booleans, and placements (move, rotate). Editing a dimension later ("make it 10 mm
taller") changes the primitive deep in the tree and everything above it follows, the way a
parametric CAD model behaves. This module has no geometry and imports only the standard
library and ops.py, so FreeCAD's own Python can load it too (worker.py).
"""
from __future__ import annotations

import math

from . import ops


class TreeError(ValueError):
    pass


def rotate_point(p: tuple, axis: str, deg: float, center: tuple) -> tuple:
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    x, y, z = p[0] - center[0], p[1] - center[1], p[2] - center[2]
    if axis == "x":
        y, z = y * c - z * s, y * s + z * c
    elif axis == "y":
        x, z = x * c + z * s, -x * s + z * c
    else:
        x, y = x * c - y * s, x * s + y * c
    return (x + center[0], y + center[1], z + center[2])


class FeatureTree:
    """The document as a parametric feature tree; subclasses evaluate geometry (bbox, measure, mesh)."""

    name = "tree"

    def __init__(self):
        self.doc = ""
        self.objects: dict[str, dict] = {}   # name -> feature
        self.visible: list[str] = []         # top-level objects (results), in creation order

    # ---- dispatch ------------------------------------------------------------------------
    def call(self, op: str, args: dict) -> dict:
        ops.validate(op, args)
        fn = getattr(self, f"op_{op}", None)
        if fn is None:
            raise TreeError(f"unknown operation {op}")
        return fn(**args)

    def _feature_bbox(self, name: str) -> tuple[tuple, tuple]:
        raise NotImplementedError

    def _bbox(self, name: str) -> tuple[tuple, tuple]:
        self._need(name)
        return self._feature_bbox(name)

    def close(self) -> None:
        pass

    # ---- documents ----------------------------------------------------------------------
    def op_new_document(self, name: str) -> dict:
        self.doc, self.objects, self.visible = name, {}, []
        return {"document": name}

    def op_list_objects(self) -> dict:
        return {"document": self.doc, "objects": [{"name": n, "type": self.objects[n]["type"]} for n in self.visible]}

    def _need_doc(self) -> None:
        if not self.doc:
            raise TreeError("no document yet: call new_document first")

    def _need(self, name: str) -> dict:
        if name not in self.objects or name not in self.visible:
            raise TreeError(f"no object named {name!r}; objects: {', '.join(self.visible) or 'none'}")
        return self.objects[name]

    def _add(self, name: str, feature: dict) -> dict:
        self._need_doc()
        if name in self.objects:
            raise TreeError(f"an object named {name!r} already exists")
        feature.setdefault("transforms", [])
        self.objects[name] = feature
        self.visible.append(name)
        return {"created": name}

    def _replace(self, name: str, feature: dict) -> dict:
        """A modifying operation (hole, shell, fillet...): the old feature becomes its hidden base."""
        base = self._need(name)
        n = 1
        while f"{name}_base{n}" in self.objects:
            n += 1
        hidden = f"{name}_base{n}"
        self.objects[hidden] = base
        feature["base"] = hidden
        feature.setdefault("transforms", [])
        self.objects[name] = feature
        return {"changed": name}

    # ---- primitives ---------------------------------------------------------------------
    def op_box(self, name, length, width, height, x=0.0, y=0.0, z=0.0) -> dict:
        return self._add(name, {"type": "box", "length": length, "width": width, "height": height, "x": x, "y": y, "z": z})

    def op_cylinder(self, name, radius, height, x=0.0, y=0.0, z=0.0, axis="z") -> dict:
        return self._add(name, {"type": "cylinder", "radius": radius, "height": height, "x": x, "y": y, "z": z, "axis": axis})

    def op_sketch_extrude(self, name, points, height, plane="XY", x=0.0, y=0.0, z=0.0) -> dict:
        return self._add(name, {"type": "extrude", "points": [list(map(float, p)) for p in points], "height": height,
                                "plane": plane, "x": x, "y": y, "z": z})

    # ---- modifications ------------------------------------------------------------------
    def op_hole(self, target, diameter, x, y, depth=0.0) -> dict:
        lo, hi = self._bbox(target)
        if not (lo[0] - 1e-6 <= x <= hi[0] + 1e-6 and lo[1] - 1e-6 <= y <= hi[1] + 1e-6):
            raise TreeError(f"hole centre ({x}, {y}) is outside {target} (x {lo[0]}..{hi[0]}, y {lo[1]}..{hi[1]})")
        return self._replace(target, {"type": "hole", "diameter": diameter, "x": x, "y": y, "depth": depth,
                                      "top": hi[2], "bottom": lo[2]})

    def op_hollow(self, target, thickness, open_face="top") -> dict:
        lo, hi = self._bbox(target)
        if 2 * thickness >= min(hi[0] - lo[0], hi[1] - lo[1]):
            raise TreeError(f"walls of {thickness} mm don't fit inside {target}")
        return self._replace(target, {"type": "hollow", "thickness": thickness, "open_face": open_face,
                                      "lo": list(lo), "hi": list(hi)})

    def op_fillet(self, target, radius, edges="all") -> dict:
        self._need(target)
        return self._replace(target, {"type": "fillet", "radius": radius, "edges": edges})

    def op_chamfer(self, target, size, edges="all") -> dict:
        self._need(target)
        return self._replace(target, {"type": "chamfer", "size": size, "edges": edges})

    def op_boolean(self, operation, base, tool, result) -> dict:
        self._need(base), self._need(tool)
        if result in self.objects and result not in (base, tool):
            raise TreeError(f"an object named {result!r} already exists")
        feature = {"type": operation, "base": base, "tool": tool, "transforms": []}
        self.visible = [n for n in self.visible if n not in (base, tool)]
        # keep the inputs reachable under hidden names when the result reuses one of their names
        for inp in (base, tool):
            if inp == result:
                hidden = f"{inp}_input"
                self.objects[hidden] = self.objects[inp]
                feature["base" if inp == base else "tool"] = hidden
        self.objects[result] = feature
        self.visible.append(result)
        return {"created": result, "consumed": [base, tool]}

    def op_set_dimension(self, name, parameter, value) -> dict:
        self._need(name)
        feature, path = self.objects[name], [name]
        while parameter not in feature and "base" in feature:   # walk down to the primitive that has it
            path.append(feature["base"])
            feature = self.objects[feature["base"]]
        if parameter not in feature or parameter in ("type", "transforms", "points", "plane", "axis"):
            raise TreeError(f"{name} has no dimension {parameter!r}")
        old = feature[parameter]
        feature[parameter] = float(value)
        self._refresh(name)
        return {"changed": name, "parameter": parameter, "old": old, "new": float(value), "in": path[-1]}

    def _refresh(self, name: str) -> None:
        """Holes and shells remember the base's top/bottom/extent; update them after a dimension change."""
        chain = []
        f = self.objects[name]
        while True:
            chain.append(f)
            if "base" not in f or f["type"] in ("cut", "fuse", "common"):
                break
            f = self.objects[f["base"]]
        for f in reversed(chain):
            if f["type"] in ("hole", "hollow"):
                lo, hi = self._feature_bbox(f["base"])
                if f["type"] == "hole":
                    f["top"], f["bottom"] = hi[2], lo[2]
                else:
                    f["lo"], f["hi"] = list(lo), list(hi)

    def op_move(self, name, dx=0.0, dy=0.0, dz=0.0) -> dict:
        self._need(name)["transforms"].append({"move": [dx, dy, dz]})
        return {"changed": name}

    def op_rotate(self, name, axis, angle, cx=None, cy=None, cz=None) -> dict:
        lo, hi = self._bbox(name)
        center = [cx if cx is not None else lo[0], cy if cy is not None else lo[1], cz if cz is not None else lo[2]]
        self._need(name)["transforms"].append({"rotate": [axis, angle, center]})
        return {"changed": name}

    def features(self, name: str) -> dict:
        """Holes, shells, fillets and sketch profiles in this object's history (newest first)."""
        out = {"holes": [], "shells": [], "fillets": [], "chamfers": [], "profiles": []}
        stack = [name]
        while stack:
            f = self.objects[stack.pop()]
            t = f["type"]
            if t == "hole":
                out["holes"].append({"diameter": f["diameter"], "x": f["x"], "y": f["y"], "axis": "z",
                                     "through": f["depth"] <= 0, "depth": f["depth"]})
            elif t == "cut" and self.objects[f["tool"]]["type"] == "cylinder" and not self.objects[f["tool"]].get("transforms"):
                c = self.objects[f["tool"]]   # a cylinder cut out of a part is a hole too (e.g. through a side wall)
                out["holes"].append({"diameter": 2 * c["radius"], "x": c["x"], "y": c["y"], "z": c["z"],
                                     "axis": c["axis"], "through": None, "depth": c["height"]})
            elif t == "hollow":
                out["shells"].append({"thickness": f["thickness"], "open_face": f["open_face"]})
            elif t == "fillet":
                out["fillets"].append({"radius": f["radius"], "edges": f["edges"]})
            elif t == "chamfer":
                out["chamfers"].append({"size": f["size"], "edges": f["edges"]})
            elif t == "extrude":
                out["profiles"].append({"plane": f["plane"], "points": f["points"], "height": f["height"]})
            for key in ("base", "tool") if t != "cut" else ("base",):   # a cut's tool isn't part of the result
                if key in f:
                    stack.append(f[key])
        return out

