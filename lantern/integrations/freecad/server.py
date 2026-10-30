"""Lantern's FreeCAD MCP server (stdio). New code; drives FreeCAD through typed operations only.

    python -m lantern.integrations.freecad.server

Lantern's MCP client starts it from ~/.lantern/mcp.json (see config/mcp/freecad.example.json)
and offers its tools to Muse Spark as deferred tools found with tool_search.

Environment (set by connector.server_spec, never secrets):
  LANTERN_FREECAD_BACKEND   auto | freecad | fake        (default auto)
  LANTERN_FREECAD_CMD       path to freecadcmd / FreeCADCmd (optional, else searched)
  LANTERN_FREECAD_ZONES     Lantern's zones.json          (privacy zones, enforced here too)
  LANTERN_FREECAD_ROOTS     Lantern's roots (os.pathsep)  (used when there is no zones.json)
  LANTERN_FREECAD_RENDERS   folder for PNG renders        (Lantern's own media folder)

Safety in this process (on top of Lantern's approvals): no arbitrary code, every file path must
be in a writable zone with the right extension, existing files are only replaced with
overwrite=true, files are written through lantern.policy and read back to verify.
"""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from typing import Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError as MCPToolError

from ...policy import Policy
from . import ops, tiers
from .backend import BackendError, make_backend
from .tree import TreeError

INSTRUCTIONS = ("Lantern's FreeCAD tools. Units are millimetres; X = length, Y = width/depth, Z = height; the "
                "origin is the part's front-left-bottom corner unless you place it elsewhere. Start with "
                "new_document. Each change returns the part's measurements and a render. When the part is done, "
                "call check_model with what the user asked for, then export if they want a file.")
MAGIC = {"stl": (b"solid", None), "step": (b"ISO-10303-21", None), "dxf": (b"0", b"SECTION"), "fcstd": (b"PK", b"{")}


class Session:
    """One CAD session: a backend plus the policy that guards files."""

    def __init__(self, backend, policy: Policy, renders: Path):
        self.backend, self.policy, self.renders = backend, policy, Path(renders)
        self.renders.mkdir(parents=True, exist_ok=True)

    def run(self, op: str, args: dict) -> dict:
        args = {k: v for k, v in args.items() if v is not None}
        try:
            ops.validate(op, args)
            return self.backend.call(op, args)
        except (ops.OpError, TreeError, BackendError) as e:
            raise MCPToolError(str(e)) from e

    def render(self, names: list[str] | None = None) -> str:
        """Render the model to a PNG in Lantern's media folder; returns its path."""
        png = self.run("render", {"names": names} if names else {})["png"]
        path = self.renders / f"freecad-{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 10**6:06d}.png"
        path.write_bytes(png if isinstance(png, (bytes, bytearray)) else bytes(png))
        return str(path)

    def after_change(self, result: dict, name: str | None) -> dict:
        """Every change answers with fresh numbers and a picture, so mistakes show up at once."""
        out = dict(result)
        if name:
            m = self.run("measure", {"name": name})
            out["measure"] = {k: m[k] for k in ("bbox", "volume", "solids", "is_valid", "approx", "features") if k in m}
        out["render"] = self.render()
        return out

    def write_file(self, kind: str, path: str, overwrite: bool, produce: Callable[[str], dict]) -> dict:
        """Zone check -> backend writes to a temp file -> policy writes the final file -> read back."""
        if kind not in ("stl", "step", "dxf", "fcstd"):
            raise MCPToolError("format must be stl, step or dxf")
        try:
            final = tiers.checked_path(self.policy, path, kind if kind != "fcstd" else "save")
        except tiers.TierError as e:
            raise MCPToolError(str(e)) from e
        existed = self.policy.exists(final)
        if existed and not overwrite:
            raise MCPToolError(f"{final} already exists; ask the user, then call again with overwrite=true")
        with tempfile.TemporaryDirectory(prefix="lantern-freecad-") as tmp:
            temp = Path(tmp) / ("out" + final.suffix)
            res = produce(str(temp))
            data = res.get("bytes")
            if data is None:
                data = temp.read_bytes() if temp.exists() else b""
            if isinstance(data, str):
                data = data.encode()
        if not data:
            raise MCPToolError("FreeCAD produced an empty file")
        self.policy.write_bytes(final, bytes(data))
        back = self.policy.read_bytes(final) if self.policy.exists(final) else b""
        head, alt = MAGIC.get(kind, (b"", None))
        ok = back == bytes(data) and (back.lstrip().startswith(head) or (alt is not None and alt in back[:4096])
                                      or (kind == "stl" and len(back) >= 84))   # binary STL from FreeCAD
        return {"path": str(final), "format": kind, "bytes": len(back), "replaced": existed, "verified": ok,
                "backend": getattr(self.backend, "name", "?")}


def session_from_env(env: dict | None = None) -> Session:
    env = dict(os.environ if env is None else env)
    roots = [r for r in env.get("LANTERN_FREECAD_ROOTS", "").split(os.pathsep) if r]
    zones = env.get("LANTERN_FREECAD_ZONES", "")
    if not roots and not (zones and Path(zones).exists()):
        raise SystemExit("Lantern's FreeCAD server needs LANTERN_FREECAD_ROOTS or LANTERN_FREECAD_ZONES")
    policy = Policy.load(zones or "/nonexistent/zones.json", roots)
    renders = Path(env.get("LANTERN_FREECAD_RENDERS") or Path(tempfile.gettempdir()) / "lantern-freecad-renders")
    return Session(make_backend(env.get("LANTERN_FREECAD_BACKEND", "auto"), env), policy, renders)


def build_server(session: Session) -> MCPServer:
    server = MCPServer("freecad", instructions=INSTRUCTIONS)
    s = session

    def tool(tier: str, description: str):
        return server.tool(description=f"[{tier}] {description}")

    # ---- green: look only --------------------------------------------------------------------
    @tool("green", "List the objects in the open FreeCAD document.")
    def list_objects() -> dict:
        return s.run("list_objects", {})

    @tool("green", "Measure an object: bounding box (mm), volume (mm³), solids, is_valid, holes and other features.")
    def measure(name: str) -> dict:
        return s.run("measure", {"name": name})

    @tool("green", "Distance between two objects in mm, and whether they intersect.")
    def distance(a: str, b: str) -> dict:
        return s.run("distance", {"a": a, "b": b})

    @tool("green", "Render the model (isometric, front and top views) to a PNG; returns its path.")
    def render_view(names: list[str] | None = None) -> dict:
        return {"render": s.render(names)}

    @tool("green", "Check the finished part against what the user asked: pass the object name and the numbers to "
                   "check (size_x/size_y/size_z in mm, hole_count, hole_diameter, wall, angle_deg). Returns numeric "
                   "checks and a render for the visual check.")
    def check_model(name: str, size_x: float | None = None, size_y: float | None = None, size_z: float | None = None,
                    hole_count: int | None = None, hole_diameter: float | None = None, wall: float | None = None,
                    angle_deg: float | None = None) -> dict:
        from .verify import numeric_checks
        m = s.run("measure", {"name": name})
        expect = {"size": {"x": size_x, "y": size_y, "z": size_z}, "holes": {"count": hole_count, "diameter": hole_diameter},
                  "wall": wall, "angle_deg": angle_deg}
        checks = [c.as_dict() for c in numeric_checks(expect, m)]
        return {"name": name, "passed": all(c["passed"] for c in checks), "checks": checks, "measure": m,
                "render": s.render(), "expect": expect}

    # ---- yellow: change the model ------------------------------------------------------------
    @tool("yellow", "Start a new, empty FreeCAD document (replaces the open one in this session).")
    def new_document(name: str) -> dict:
        return s.run("new_document", {"name": name})

    @tool("yellow", "Add a box. length=X, width=Y, height=Z in mm; x, y, z place its front-left-bottom corner.")
    def box(name: str, length: float, width: float, height: float, x: float = 0, y: float = 0, z: float = 0) -> dict:
        return s.after_change(s.run("box", dict(name=name, length=length, width=width, height=height, x=x, y=y, z=z)), name)

    @tool("yellow", "Add a cylinder (radius, height in mm). x, y, z = centre of its base; axis x, y or z.")
    def cylinder(name: str, radius: float, height: float, x: float = 0, y: float = 0, z: float = 0, axis: str = "z") -> dict:
        return s.after_change(s.run("cylinder", dict(name=name, radius=radius, height=height, x=x, y=y, z=z, axis=axis)), name)

    @tool("yellow", "Draw a closed 2D profile (3-64 [u, v] points in mm) on a plane and extrude it. Planes: XY "
                    "(u=X, v=Y, extrudes up Z), XZ (u=X, v=Z, extrudes along +Y), YZ (u=Y, v=Z, along +X). "
                    "x, y, z offset the sketch. Use this for wedges, stands and brackets.")
    def sketch_extrude(name: str, points: list[list[float]], height: float, plane: str = "XY",
                       x: float = 0, y: float = 0, z: float = 0) -> dict:
        return s.after_change(s.run("sketch_extrude", dict(name=name, points=points, height=height, plane=plane,
                                                           x=x, y=y, z=z)), name)

    @tool("yellow", "Drill a vertical hole down from the top of an object at (x, y) mm. depth 0 = through.")
    def hole(target: str, diameter: float, x: float, y: float, depth: float = 0) -> dict:
        return s.after_change(s.run("hole", dict(target=target, diameter=diameter, x=x, y=y, depth=depth)), target)

    @tool("yellow", "Hollow out an object to walls of `thickness` mm, leaving one face open (top, bottom or none).")
    def hollow(target: str, thickness: float, open_face: str = "top") -> dict:
        return s.after_change(s.run("hollow", dict(target=target, thickness=thickness, open_face=open_face)), target)

    @tool("yellow", "Round edges (all, vertical, top or bottom) with a radius in mm.")
    def fillet(target: str, radius: float, edges: str = "all") -> dict:
        return s.after_change(s.run("fillet", dict(target=target, radius=radius, edges=edges)), target)

    @tool("yellow", "Bevel edges (all, vertical, top or bottom) by a size in mm.")
    def chamfer(target: str, size: float, edges: str = "all") -> dict:
        return s.after_change(s.run("chamfer", dict(target=target, size=size, edges=edges)), target)

    @tool("yellow", "Combine two objects: fuse (join), cut (base minus tool) or common (overlap) into `result`.")
    def boolean(operation: str, base: str, tool: str, result: str) -> dict:
        return s.after_change(s.run("boolean", dict(operation=operation, base=base, tool=tool, result=result)), result)

    @tool("yellow", "Change a dimension of an existing part (length, width, height, radius, thickness, diameter...). "
                    "Holes and shells follow, like a parametric model.")
    def set_dimension(name: str, parameter: str, value: float) -> dict:
        return s.after_change(s.run("set_dimension", dict(name=name, parameter=parameter, value=value)), name)

    @tool("yellow", "Move an object by dx, dy, dz mm.")
    def move(name: str, dx: float = 0, dy: float = 0, dz: float = 0) -> dict:
        return s.after_change(s.run("move", dict(name=name, dx=dx, dy=dy, dz=dz)), name)

    @tool("yellow", "Rotate an object by `angle` degrees about the x, y or z axis through (cx, cy, cz) "
                    "(default: its front-left-bottom corner).")
    def rotate(name: str, axis: str, angle: float, cx: float | None = None, cy: float | None = None,
               cz: float | None = None) -> dict:
        return s.after_change(s.run("rotate", dict(name=name, axis=axis, angle=angle, cx=cx, cy=cy, cz=cz)), name)

    @tool("yellow", "Save the model as a FreeCAD .FCStd file in a writable folder (red if it replaces a file: "
                    "overwrite=true only after the user agreed).")
    def save(path: str, overwrite: bool = False) -> dict:
        return s.write_file("fcstd", path, overwrite, lambda tmp: s.run("save", {"path": tmp}))

    # ---- red: files for the outside world ----------------------------------------------------
    @tool("red", "Export objects as STL (3D printing), STEP (CAD exchange) or DXF (top-view outline) to a file in a "
                 "writable folder. The user approves the exact file first. overwrite=true only after they agreed.")
    def export(names: list[str], path: str, format: str, overwrite: bool = False) -> dict:
        format = str(format).lower()   # live run, Oct 27: Muse Spark wrote "STL"
        return s.write_file(format, path, overwrite,
                            lambda tmp: s.run("export", {"names": names, "path": tmp, "format": format}))

    return server


def main() -> None:
    session = session_from_env()
    build_server(session).run()   # stdio


if __name__ == "__main__":
    main()
