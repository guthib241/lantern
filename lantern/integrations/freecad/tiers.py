"""Risk tiers for the FreeCAD tools (docs/PHASE2_SIGNATURE.md S0.3), enforced in code.

  green   look only (measure, distance, list, render, check): runs automatically
  yellow  create or change parts in the open document (reversible in the model): one-tap OK
  red     anything that writes a file outside the model: every export (STL, STEP, DXF), and a
          save that would replace an existing file. The approval lists the exact files.

File paths are checked against Lantern's privacy zones before anything runs: only writable
zones, never hidden or private ones. Saving to a new .FCStd file is yellow; replacing one is red.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from ...policy import Policy, PolicyError

GREEN, YELLOW, RED = "green", "yellow", "red"

TIERS = {
    "list_objects": GREEN, "measure": GREEN, "distance": GREEN, "render_view": GREEN, "check_model": GREEN,
    "new_document": YELLOW, "box": YELLOW, "cylinder": YELLOW, "sketch_extrude": YELLOW, "hole": YELLOW,
    "hollow": YELLOW, "fillet": YELLOW, "chamfer": YELLOW, "boolean": YELLOW, "set_dimension": YELLOW,
    "move": YELLOW, "rotate": YELLOW,
    "save": YELLOW,     # becomes red when it would replace a file
    "export": RED,
}
GREEN_TOOLS = sorted(t for t, tier in TIERS.items() if tier == GREEN)
EXTENSIONS = {"stl": ".stl", "step": ".step", "dxf": ".dxf", "save": ".fcstd"}


class TierError(ValueError):
    """Refused before running (a path outside writable zones, a wrong extension...)."""


@dataclass
class Review:
    tool: str
    tier: str
    summary: str
    payload: dict
    files: list[str] = field(default_factory=list)   # absolute paths this call will write
    overwrites: list[str] = field(default_factory=list)


def _hash(args: dict) -> str:
    return hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()


def checked_path(policy: Policy, path: str, kind: str) -> Path:
    """A writable-zone path with the right extension, resolved (symlinks followed)."""
    if kind not in EXTENSIONS:
        raise TierError(f"unknown file kind {kind!r}")
    if not isinstance(path, str) or not path.strip():
        raise TierError("a file path is needed")
    try:
        p = policy.check_writable(path)
    except PolicyError as e:
        raise TierError(str(e)) from e
    want = EXTENSIONS[kind]
    if p.suffix.lower() not in (want, ".stp" if want == ".step" else want):
        raise TierError(f"{p.name}: a {kind.upper() if kind != 'save' else 'FreeCAD'} file should end in {want}")
    return p


def describe(tool: str, args: dict) -> str:
    a = args
    if tool == "box":
        return f"Add a box '{a['name']}' {a['length']:g} × {a['width']:g} × {a['height']:g} mm"
    if tool == "cylinder":
        return f"Add a cylinder '{a['name']}' Ø{2 * a['radius']:g} × {a['height']:g} mm"
    if tool == "sketch_extrude":
        return f"Add '{a['name']}': a {len(a['points'])}-point profile extruded {a['height']:g} mm"
    if tool == "hole":
        depth = "through" if not a.get("depth") else f"{a['depth']:g} mm deep"
        return f"Drill a Ø{a['diameter']:g} mm hole ({depth}) in '{a['target']}' at ({a['x']:g}, {a['y']:g})"
    if tool == "hollow":
        return f"Hollow '{a['target']}' to {a['thickness']:g} mm walls (open {a.get('open_face', 'top')})"
    if tool in ("fillet", "chamfer"):
        size = a.get("radius", a.get("size"))
        return f"{tool.capitalize()} the {a.get('edges', 'all')} edges of '{a['target']}' ({size:g} mm)"
    if tool == "boolean":
        return f"{a['operation'].capitalize()} '{a['tool']}' with '{a['base']}' into '{a['result']}'"
    if tool == "set_dimension":
        return f"Set {a['parameter']} of '{a['name']}' to {a['value']:g} mm"
    if tool == "move":
        return f"Move '{a['name']}' by ({a.get('dx', 0):g}, {a.get('dy', 0):g}, {a.get('dz', 0):g}) mm"
    if tool == "rotate":
        return f"Rotate '{a['name']}' {a['angle']:g}° about {a['axis']}"
    if tool == "new_document":
        return f"Start a new FreeCAD document '{a['name']}'"
    return f"FreeCAD: {tool}"


def review(tool: str, args: dict, policy: Policy, *, exists=lambda p: p.exists(), checks: dict | None = None) -> Review:
    """What this call would do, its tier, and the approval payload (bound to the exact arguments)."""
    if tool not in TIERS:
        raise TierError(f"unknown FreeCAD tool {tool}")
    tier = TIERS[tool]
    payload = {"tool": f"freecad.{tool}", "args_sha256": _hash(args)}
    if tool == "export":
        fmt = str(args.get("format", "")).lower()
        if fmt not in ("stl", "step", "dxf"):
            raise TierError("format must be stl, step or dxf")
        args = {**args, "format": fmt}
        p = checked_path(policy, str(args.get("path", "")), fmt)
        payload.update(file=str(p), format=args["format"], objects=list(args["names"]))
        over = [str(p)] if exists(p) else []
        verdict = ""
        if checks is not None:
            verdict = " Checks: " + ("passed ✓" if checks.get("passed") else "NOT passed ✗ (" +
                                     "; ".join(checks.get("failed", [])[:3]) + ")")
        summary = (f"Export {', '.join(args['names'])} as {args['format'].upper()} to {p}"
                   + (" (REPLACES the existing file)" if over else "") + "." + verdict)
        return Review(tool, RED, summary, payload, [str(p)], over)
    if tool == "save":
        p = checked_path(policy, str(args.get("path", "")), "save")
        payload.update(file=str(p))
        if exists(p):
            return Review(tool, RED, f"Save the FreeCAD model to {p} (REPLACES the existing file).", payload, [str(p)], [str(p)])
        return Review(tool, YELLOW, f"Save the FreeCAD model to {p} (new file).", payload, [str(p)])
    return Review(tool, tier, describe(tool, args), payload)
