"""The CAD operations Lantern may ask for, and their argument rules.

One table, used by the MCP server (to build typed tools), the fake backend and the FreeCAD
worker's caller, so a bad request is refused the same way everywhere and long before FreeCAD
sees it. There is deliberately no "run this Python" operation: every action is typed, so file
paths can be checked against privacy zones and shown in approvals.
"""
from __future__ import annotations

import math
import re
from typing import Any

MAX_MM = 2000.0                 # nothing bigger than 2 m
NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,40}$")
FORMATS = ("stl", "step", "dxf")
EDGES = ("all", "vertical", "top", "bottom")


class OpError(ValueError):
    """A request that breaks the rules. The message is safe to show the model."""


def _num(v: Any, what: str, lo: float = -MAX_MM, hi: float = MAX_MM, positive: bool = False) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise OpError(f"{what} must be a number")
    if positive and v <= 0:
        raise OpError(f"{what} must be more than 0")
    if not lo <= v <= hi:
        raise OpError(f"{what} must be between {lo:g} and {hi:g}")
    return float(v)


def _name(v: Any, what: str = "name") -> str:
    if not isinstance(v, str) or not NAME.match(v):
        raise OpError(f"{what} must start with a letter and use only letters, digits and _ (max 41)")
    return v


def _choice(v: Any, what: str, options: tuple) -> str:
    if v not in options:
        raise OpError(f"{what} must be one of {', '.join(options)}")
    return v


def _points(v: Any) -> None:
    if not isinstance(v, list) or not 3 <= len(v) <= 64:
        raise OpError("points must be a list of 3 to 64 [u, v] pairs in mm")
    for i, p in enumerate(v):
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            raise OpError(f"point {i} must be [u, v]")
        _num(p[0], f"point {i} u"), _num(p[1], f"point {i} v")
    area = sum(v[i][0] * v[(i + 1) % len(v)][1] - v[(i + 1) % len(v)][0] * v[i][1] for i in range(len(v))) / 2
    if abs(area) < 1e-6:
        raise OpError("the points don't enclose an area")


# op -> {arg: (kind, required)}; kinds: name, pos (mm > 0), mm, deg, depth (>= 0), axis, plane, op, edges,
# format, names, points, path, bool, side
SPEC: dict[str, dict[str, tuple[str, bool]]] = {
    "new_document": {"name": ("name", True)},
    "list_objects": {},
    "box": {"name": ("name", True), "length": ("pos", True), "width": ("pos", True), "height": ("pos", True),
            "x": ("mm", False), "y": ("mm", False), "z": ("mm", False)},
    "cylinder": {"name": ("name", True), "radius": ("pos", True), "height": ("pos", True),
                 "x": ("mm", False), "y": ("mm", False), "z": ("mm", False), "axis": ("axis", False)},
    "sketch_extrude": {"name": ("name", True), "points": ("points", True), "height": ("pos", True),
                       "plane": ("plane", False), "x": ("mm", False), "y": ("mm", False), "z": ("mm", False)},
    "hole": {"target": ("name", True), "diameter": ("pos", True), "x": ("mm", True), "y": ("mm", True),
             "depth": ("depth", False)},
    "hollow": {"target": ("name", True), "thickness": ("pos", True), "open_face": ("side", False)},
    "fillet": {"target": ("name", True), "radius": ("pos", True), "edges": ("edges", False)},
    "chamfer": {"target": ("name", True), "size": ("pos", True), "edges": ("edges", False)},
    "boolean": {"operation": ("op", True), "base": ("name", True), "tool": ("name", True), "result": ("name", True)},
    "set_dimension": {"name": ("name", True), "parameter": ("param", True), "value": ("pos", True)},
    "move": {"name": ("name", True), "dx": ("mm", False), "dy": ("mm", False), "dz": ("mm", False)},
    "rotate": {"name": ("name", True), "axis": ("axis", True), "angle": ("deg", True),
               "cx": ("mm", False), "cy": ("mm", False), "cz": ("mm", False)},
    "measure": {"name": ("name", True)},
    "distance": {"a": ("name", True), "b": ("name", True)},
    "mesh": {"names": ("names", False)},
    "render": {"names": ("names", False)},
    "save": {"path": ("path", True)},
    "export": {"names": ("names", True), "path": ("path", True), "format": ("format", True)},
}
PARAMS = ("length", "width", "height", "radius", "diameter", "thickness", "depth", "size", "x", "y", "z")


def validate(op: str, args: dict) -> None:
    spec = SPEC.get(op)
    if spec is None:
        raise OpError(f"unknown operation {op}")
    extra = set(args) - set(spec)
    if extra:
        raise OpError(f"{op} doesn't take {', '.join(sorted(extra))}")
    for arg, (kind, required) in spec.items():
        if arg not in args or args[arg] is None:
            if required:
                raise OpError(f"{op} needs {arg}")
            continue
        v = args[arg]
        if kind == "name":
            _name(v, arg)
        elif kind == "pos":
            _num(v, arg, 0, MAX_MM, positive=True)
        elif kind == "mm":
            _num(v, arg)
        elif kind == "depth":
            _num(v, arg, 0, MAX_MM)
        elif kind == "deg":
            _num(v, arg, -360, 360)
        elif kind == "axis":
            _choice(v, arg, ("x", "y", "z"))
        elif kind == "plane":
            _choice(v, arg, ("XY", "XZ", "YZ"))
        elif kind == "op":
            _choice(v, arg, ("fuse", "cut", "common"))
        elif kind == "edges":
            _choice(v, arg, EDGES)
        elif kind == "side":
            _choice(v, arg, ("top", "bottom", "none"))
        elif kind == "format":
            _choice(v, arg, FORMATS)
        elif kind == "param":
            _choice(v, arg, PARAMS)
        elif kind == "points":
            _points(v)
        elif kind == "names":
            if not isinstance(v, list) or not 1 <= len(v) <= 20:
                raise OpError(f"{arg} must be a list of 1 to 20 object names")
            for n in v:
                _name(n, arg)
        elif kind == "path":
            if not isinstance(v, str) or not v.strip() or "\x00" in v:
                raise OpError(f"{arg} must be a file path")


def bbox_dict(lo, hi) -> dict:
    r = lambda v: round(float(v), 3)  # noqa: E731
    return {"xmin": r(lo[0]), "ymin": r(lo[1]), "zmin": r(lo[2]), "xmax": r(hi[0]), "ymax": r(hi[1]),
            "zmax": r(hi[2]), "x": r(hi[0] - lo[0]), "y": r(hi[1] - lo[1]), "z": r(hi[2] - lo[2])}


# ---- small file writers used by the fake backend (the real worker uses FreeCAD's own exporters) ----
def stl_ascii(tris, name: str = "lantern") -> bytes:
    from .render import _cross, _norm, _sub
    lines = [f"solid {name}"]
    for t in tris:
        n = _norm(_cross(_sub(t[1], t[0]), _sub(t[2], t[0])))
        lines.append(f"  facet normal {n[0]:.4f} {n[1]:.4f} {n[2]:.4f}\n    outer loop")
        lines += [f"      vertex {v[0]:.4f} {v[1]:.4f} {v[2]:.4f}" for v in t]
        lines.append("    endloop\n  endfacet")
    lines.append(f"endsolid {name}")
    return ("\n".join(lines) + "\n").encode()


def step_placeholder(names: list[str], boxes) -> bytes:
    """Not a real STEP solid: the fake backend can't make one. Says so in the file itself."""
    body = "\n".join(f"/* {n}: bounding box {bbox_dict(lo, hi)} */" for n, (lo, hi) in zip(names, boxes))
    return ("ISO-10303-21;\nHEADER;\nFILE_DESCRIPTION(('Lantern fake backend: bounding boxes only, not a "
            "real solid'),'2;1');\nENDSEC;\nDATA;\n" + body + "\nENDSEC;\nEND-ISO-10303-21;\n").encode()


def dxf_lines(lines) -> bytes:
    """2D line segments [((x0, y0), (x1, y1)), ...] as a minimal DXF (ENTITIES section only)."""
    out = ["0", "SECTION", "2", "ENTITIES"]
    for (x0, y0), (x1, y1) in lines:
        out += ["0", "LINE", "8", "0", "10", f"{x0:.3f}", "20", f"{y0:.3f}", "11", f"{x1:.3f}", "21", f"{y1:.3f}"]
    out += ["0", "ENDSEC", "0", "EOF"]
    return ("\n".join(out) + "\n").encode()


def dxf_outline(boxes) -> bytes:
    """Top-view outlines (rectangles) as a minimal DXF (the fake backend's stand-in)."""
    lines = []
    for lo, hi in boxes:
        pts = [(lo[0], lo[1]), (hi[0], lo[1]), (hi[0], hi[1]), (lo[0], hi[1])]
        lines += list(zip(pts, pts[1:] + pts[:1]))
    return dxf_lines(lines)
