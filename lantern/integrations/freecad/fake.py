"""A stand-in for FreeCAD, for tests and the cloud sandbox (no FreeCAD install needed).

It keeps the same parametric feature tree the real worker builds in a FreeCAD document
(primitives, extrusions, holes, shells, booleans, fillets, placements) and answers the same
operations. Geometry is evaluated by point membership: bounding boxes are exact for the shapes
it supports, volume and "is it one solid" come from a voxel grid (marked approx), and renders
are the voxel surface drawn by render.py. It is honest about being a stand-in: every measure
says backend "fake".
"""
from __future__ import annotations

import math
from collections import deque

from . import ops
from .render import Tri, render
from .tree import FeatureTree, TreeError, rotate_point

GRID = 64          # voxels along the longest side for measuring (volume is approx, about ±6%)
RENDER_GRID = 40   # and for rendering


FakeError = TreeError


def _point_in_polygon(u: float, v: float, poly: list[list[float]]) -> bool:
    inside, j = False, len(poly) - 1
    for i in range(len(poly)):
        ui, vi = poly[i]
        uj, vj = poly[j]
        if (vi > v) != (vj > v) and u < (uj - ui) * (v - vi) / ((vj - vi) or 1e-12) + ui:
            inside = not inside
        j = i
    return inside


class FakeBackend(FeatureTree):
    """FeatureTree + voxel geometry: no FreeCAD needed."""

    name = "fake"

    def __init__(self):
        super().__init__()
        self.exports: dict[str, bytes] = {}  # last exported bytes per path (for tests)

    # ---- geometry -----------------------------------------------------------------------
    def _local(self, f: dict, p: tuple) -> tuple:
        for t in reversed(f.get("transforms", [])):   # undo placements, last first
            if "move" in t:
                p = (p[0] - t["move"][0], p[1] - t["move"][1], p[2] - t["move"][2])
            else:
                axis, angle, c = t["rotate"]
                p = rotate_point(p, axis, -angle, tuple(c))
        return p

    def contains(self, name: str, p: tuple) -> bool:
        f = self.objects[name]
        p = self._local(f, p)
        t = f["type"]
        if t == "box":
            return (f["x"] <= p[0] <= f["x"] + f["length"] and f["y"] <= p[1] <= f["y"] + f["width"]
                    and f["z"] <= p[2] <= f["z"] + f["height"])
        if t == "cylinder":
            ax = f["axis"]
            u, v, w = {"z": (0, 1, 2), "x": (1, 2, 0), "y": (0, 2, 1)}[ax]
            c = (f["x"], f["y"], f["z"])
            return (math.hypot(p[u] - c[u], p[v] - c[v]) <= f["radius"]
                    and c[w] <= p[w] <= c[w] + f["height"])
        if t == "extrude":
            u, v, w = {"XY": (0, 1, 2), "XZ": (0, 2, 1), "YZ": (1, 2, 0)}[f["plane"]]
            off = (f["x"], f["y"], f["z"])
            return (off[w] <= p[w] <= off[w] + f["height"]
                    and _point_in_polygon(p[u] - off[u], p[v] - off[v], f["points"]))
        if t == "hole":
            r = f["diameter"] / 2
            bottom = f["bottom"] - 1 if f["depth"] <= 0 else f["top"] - f["depth"]
            in_hole = math.hypot(p[0] - f["x"], p[1] - f["y"]) <= r and bottom <= p[2] <= f["top"] + 1
            return self.contains(f["base"], p) and not in_hole
        if t == "hollow":
            lo, hi, th = f["lo"], f["hi"], f["thickness"]
            inner = (lo[0] + th < p[0] < hi[0] - th and lo[1] + th < p[1] < hi[1] - th
                     and lo[2] + th < p[2] <= hi[2] + 1)
            return self.contains(f["base"], p) and not inner
        if t in ("fillet", "chamfer"):
            return self.contains(f["base"], p)     # the stand-in ignores the (tiny) rounding
        if t == "cut":
            return self.contains(f["base"], p) and not self.contains(f["tool"], p)
        if t == "fuse":
            return self.contains(f["base"], p) or self.contains(f["tool"], p)
        if t == "common":
            return self.contains(f["base"], p) and self.contains(f["tool"], p)
        raise FakeError(f"unknown feature type {t}")

    def _feature_bbox(self, name: str) -> tuple[tuple, tuple]:
        f = self.objects[name]
        t = f["type"]
        if t == "box":
            lo = (f["x"], f["y"], f["z"])
            hi = (f["x"] + f["length"], f["y"] + f["width"], f["z"] + f["height"])
        elif t == "cylinder":
            r, h, c = f["radius"], f["height"], [f["x"], f["y"], f["z"]]
            idx = {"z": 2, "x": 0, "y": 1}[f["axis"]]
            lo = [c[i] - r for i in range(3)]
            hi = [c[i] + r for i in range(3)]
            lo[idx], hi[idx] = c[idx], c[idx] + h
            lo, hi = tuple(lo), tuple(hi)
        elif t == "extrude":
            u, v, w = {"XY": (0, 1, 2), "XZ": (0, 2, 1), "YZ": (1, 2, 0)}[f["plane"]]
            off = (f["x"], f["y"], f["z"])
            lo, hi = [0.0] * 3, [0.0] * 3
            lo[u], hi[u] = off[u] + min(p[0] for p in f["points"]), off[u] + max(p[0] for p in f["points"])
            lo[v], hi[v] = off[v] + min(p[1] for p in f["points"]), off[v] + max(p[1] for p in f["points"])
            lo[w], hi[w] = off[w], off[w] + f["height"]
            lo, hi = tuple(lo), tuple(hi)
        elif t in ("hole", "hollow", "fillet", "chamfer", "cut"):
            lo, hi = self._feature_bbox(f["base"])
        elif t == "fuse":
            a, b = self._feature_bbox(f["base"]), self._feature_bbox(f["tool"])
            lo = tuple(min(a[0][i], b[0][i]) for i in range(3))
            hi = tuple(max(a[1][i], b[1][i]) for i in range(3))
        elif t == "common":
            a, b = self._feature_bbox(f["base"]), self._feature_bbox(f["tool"])
            lo = tuple(max(a[0][i], b[0][i]) for i in range(3))
            hi = tuple(min(a[1][i], b[1][i]) for i in range(3))
        else:
            raise FakeError(f"unknown feature type {t}")
        return self._transform_bbox(f, lo, hi)

    def _transform_bbox(self, f: dict, lo: tuple, hi: tuple) -> tuple[tuple, tuple]:
        if not f.get("transforms"):
            return lo, hi
        pts = [(x, y, z) for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])]
        for t in f["transforms"]:
            if "move" in t:
                pts = [(p[0] + t["move"][0], p[1] + t["move"][1], p[2] + t["move"][2]) for p in pts]
            else:
                axis, angle, c = t["rotate"]
                pts = [rotate_point(p, axis, angle, tuple(c)) for p in pts]
        return (tuple(min(p[i] for p in pts) for i in range(3)), tuple(max(p[i] for p in pts) for i in range(3)))

    def _grid(self, names: list[str], n: int) -> tuple[dict, float, tuple]:
        """Occupancy {(i,j,k)} of the union of `names`, cell size, origin."""
        boxes = [self._feature_bbox(nm) for nm in names]
        lo = tuple(min(b[0][i] for b in boxes) for i in range(3))
        hi = tuple(max(b[1][i] for b in boxes) for i in range(3))
        size = max(hi[i] - lo[i] for i in range(3)) or 1.0
        cell = size / n
        counts = [max(1, math.ceil((hi[i] - lo[i]) / cell)) for i in range(3)]
        occ = set()
        for i in range(counts[0]):
            for j in range(counts[1]):
                for k in range(counts[2]):
                    p = (lo[0] + (i + 0.5) * cell, lo[1] + (j + 0.5) * cell, lo[2] + (k + 0.5) * cell)
                    if any(self.contains(nm, p) for nm in names):
                        occ.add((i, j, k))
        return {"occ": occ, "counts": counts}, cell, lo

    @staticmethod
    def _components(occ: set) -> int:
        seen, comps = set(), 0
        for start in occ:
            if start in seen:
                continue
            comps += 1
            q = deque([start])
            seen.add(start)
            while q:
                i, j, k = q.popleft()
                for d in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
                    nb = (i + d[0], j + d[1], k + d[2])
                    if nb in occ and nb not in seen:
                        seen.add(nb)
                        q.append(nb)
        return comps

    # ---- measuring ----------------------------------------------------------------------
    def openings(self, name: str, hole: dict, samples: int = 400) -> int:
        """How many walls a hole passes through: material segments on a line just beside its axis."""
        lo, hi = self._bbox(name)
        ax = hole.get("axis", "z")
        r = hole["diameter"] / 2 + 0.3
        i = {"x": 0, "y": 1, "z": 2}[ax]
        base = [hole["x"], hole["y"], hole.get("z", 0.0)]
        side = 0 if ax != "x" else 1   # offset sideways, never along the axis
        base[side] += r
        segs, inside = 0, False
        for n in range(samples + 1):
            p = list(base)
            p[i] = lo[i] - 0.5 + (hi[i] - lo[i] + 1) * n / samples
            now = self.contains(name, tuple(p))
            if now and not inside:
                segs += 1
            inside = now
        return segs

    def op_measure(self, name) -> dict:
        lo, hi = self._bbox(name)
        grid, cell, _ = self._grid([name], GRID)
        occ = grid["occ"]
        solids = self._components(occ) if occ else 0
        feats = self.features(name)
        for h in feats["holes"]:
            h["openings"] = self.openings(name, h)
        return {"name": name, "backend": self.name, "approx": True,
                "bbox": ops.bbox_dict(lo, hi), "volume": round(len(occ) * cell ** 3, 1),
                "solids": solids, "is_valid": bool(occ) and solids >= 1, "features": feats}

    def op_distance(self, a, b) -> dict:
        (alo, ahi), (blo, bhi) = self._bbox(a), self._bbox(b)
        gap = math.sqrt(sum(max(0.0, blo[i] - ahi[i], alo[i] - bhi[i]) ** 2 for i in range(3)))
        overlap = 0.0
        if gap == 0:
            grid, cell, lo = self._grid([a, b], GRID)
            both = sum(1 for (i, j, k) in grid["occ"]
                       if self.contains(a, (lo[0] + (i + .5) * cell, lo[1] + (j + .5) * cell, lo[2] + (k + .5) * cell))
                       and self.contains(b, (lo[0] + (i + .5) * cell, lo[1] + (j + .5) * cell, lo[2] + (k + .5) * cell)))
            overlap = round(both * cell ** 3, 1)
        return {"a": a, "b": b, "distance": round(gap, 3), "intersecting": overlap > 0, "common_volume": overlap,
                "backend": self.name, "approx": True}

    def op_mesh(self, names=None) -> dict:
        names = names or list(self.visible)
        for n in names:
            self._need(n)
        return {"triangles": self.triangles(names), "backend": self.name}

    def triangles(self, names: list[str], n: int = RENDER_GRID) -> list[Tri]:
        if not names:
            return []
        grid, cell, lo = self._grid(names, n)
        occ, tris = grid["occ"], []
        for (i, j, k) in occ:
            x0, y0, z0 = lo[0] + i * cell, lo[1] + j * cell, lo[2] + k * cell
            x1, y1, z1 = x0 + cell, y0 + cell, z0 + cell
            for (di, dj, dk), quad in (
                ((1, 0, 0), [(x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1)]),
                ((-1, 0, 0), [(x0, y0, z0), (x0, y0, z1), (x0, y1, z1), (x0, y1, z0)]),
                ((0, 1, 0), [(x0, y1, z0), (x0, y1, z1), (x1, y1, z1), (x1, y1, z0)]),
                ((0, -1, 0), [(x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1)]),
                ((0, 0, 1), [(x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]),
                ((0, 0, -1), [(x0, y0, z0), (x0, y1, z0), (x1, y1, z0), (x1, y0, z0)]),
            ):
                if (i + di, j + dj, k + dk) not in occ:
                    a, b, c, d = quad
                    tris += [(a, b, c), (a, c, d)]
        return tris

    def op_render(self, names=None) -> dict:
        names = names or list(self.visible)
        for n in names:
            self._need(n)
        return {"png": render(self.triangles(names)), "backend": self.name}

    # ---- files --------------------------------------------------------------------------
    def op_save(self, path) -> dict:
        self._need_doc()
        import json
        data = json.dumps({"document": self.doc, "objects": self.objects, "visible": self.visible}).encode()
        return {"bytes": data, "format": "fcstd-fake"}

    def op_export(self, names, path, format) -> dict:
        for n in names:
            self._need(n)
        tris = self.triangles(names)
        if format == "stl":
            data = ops.stl_ascii(tris, " ".join(names))
        elif format == "step":
            data = ops.step_placeholder(names, [self._bbox(n) for n in names])
        else:
            data = ops.dxf_outline([self._bbox(n) for n in names])
        self.exports[path] = data
        return {"bytes": data, "format": format}
