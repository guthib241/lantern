"""A tiny software renderer for the visual verify loop: three views of a model in one PNG.

No dependencies beyond the standard library, so it runs in Lantern's Python whatever FreeCAD
ships with. Input is a triangle list in millimetres (from FreeCAD's tessellation, or from the
fake backend). Output: a PNG with an isometric view, a front view (looking along +Y) and a top
view (looking down -Z), flat shaded with a z-buffer, plus a 10 mm scale bar in each panel so the
image model can judge sizes.
"""
from __future__ import annotations

import math
import struct
import zlib
from typing import Iterable, Sequence

Vec = tuple[float, float, float]
Tri = tuple[Vec, Vec, Vec]

PANEL = 260           # pixels per view
MARGIN = 18
BG = (246, 244, 240)
INK = (60, 60, 70)
BASE = (226, 132, 58)  # lantern orange
LIGHT = (-0.45, -0.55, 0.70)

VIEWS = {
    # name: (right axis, up axis, toward-viewer axis) as unit vectors in model space
    "iso": ((0.7071, 0.7071, 0.0), (-0.4082, 0.4082, 0.8165), (0.5774, -0.5774, 0.5774)),
    "front": ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, -1.0, 0.0)),
    "top": ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
}


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _sub(a: Vec, b: Vec) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a: Vec, b: Vec) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _norm(a: Vec) -> Vec:
    n = math.sqrt(_dot(a, a)) or 1.0
    return (a[0] / n, a[1] / n, a[2] / n)


def png_bytes(width: int, height: int, rows: list[bytearray]) -> bytes:
    """Encode RGB rows as a PNG (stdlib only)."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + bytes(r) for r in rows)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


class Canvas:
    def __init__(self, width: int, height: int):
        self.w, self.h = width, height
        self.rows = [bytearray(BG * width) for _ in range(height)]
        self.depth = [[-math.inf] * width for _ in range(height)]

    def put(self, x: int, y: int, rgb: tuple[int, int, int], z: float = math.inf) -> None:
        if 0 <= x < self.w and 0 <= y < self.h and z >= self.depth[y][x]:
            self.depth[y][x] = z
            self.rows[y][3 * x:3 * x + 3] = bytes(rgb)

    def line(self, x0: int, y0: int, x1: int, y1: int, rgb=INK) -> None:
        steps = max(abs(x1 - x0), abs(y1 - y0), 1)
        for i in range(steps + 1):
            self.put(round(x0 + (x1 - x0) * i / steps), round(y0 + (y1 - y0) * i / steps), rgb)

    def triangle(self, p: list[tuple[float, float, float]], rgb: tuple[int, int, int]) -> None:
        """Fill a screen-space triangle (x, y, depth) with z-buffering (larger depth = nearer)."""
        (x0, y0, z0), (x1, y1, z1), (x2, y2, z2) = p
        area = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)
        if abs(area) < 1e-9:
            return
        xmin, xmax = max(int(min(x0, x1, x2)), 0), min(int(max(x0, x1, x2)) + 1, self.w - 1)
        ymin, ymax = max(int(min(y0, y1, y2)), 0), min(int(max(y0, y1, y2)) + 1, self.h - 1)
        for y in range(ymin, ymax + 1):
            py = y + 0.5
            for x in range(xmin, xmax + 1):
                px = x + 0.5
                w0 = ((x1 - px) * (y2 - py) - (x2 - px) * (y1 - py)) / area
                w1 = ((x2 - px) * (y0 - py) - (x0 - px) * (y2 - py)) / area
                w2 = 1.0 - w0 - w1
                if w0 >= -1e-6 and w1 >= -1e-6 and w2 >= -1e-6:
                    self.put(x, y, rgb, w0 * z0 + w1 * z1 + w2 * z2)


def bounds(tris: Iterable[Tri]) -> tuple[Vec, Vec]:
    xs, ys, zs = [], [], []
    for t in tris:
        for v in t:
            xs.append(v[0]), ys.append(v[1]), zs.append(v[2])
    if not xs:
        return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def render(tris: list[Tri], *, panel: int = PANEL) -> bytes:
    """Three-view PNG of the triangles. An empty model renders an empty (but valid) image."""
    names = list(VIEWS)
    width, height = panel * len(names), panel
    canvas = Canvas(width, height)
    lo, hi = bounds(tris)
    center = tuple((a + b) / 2 for a, b in zip(lo, hi))
    light = _norm(LIGHT)
    for i, name in enumerate(names):
        right, up, toward = VIEWS[name]
        # fit: the model's projected extent (all 8 bbox corners) into the panel
        corners = [(x, y, z) for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])]
        us = [_dot(_sub(c, center), right) for c in corners]
        vs = [_dot(_sub(c, center), up) for c in corners]
        extent = max(max(us) - min(us), max(vs) - min(vs), 1e-6)
        scale = (panel - 2 * MARGIN) / extent
        ox, oy = i * panel + panel / 2, panel / 2

        def proj(v: Vec) -> tuple[float, float, float]:
            d = _sub(v, center)
            return (ox + _dot(d, right) * scale, oy - _dot(d, up) * scale, _dot(d, toward))
        for t in tris:
            n = _cross(_sub(t[1], t[0]), _sub(t[2], t[0]))
            if _dot(n, n) < 1e-18:
                continue
            n = _norm(n)
            facing = _dot(n, toward)
            if facing < -1e-9:   # solids are closed: a face turned away is always hidden behind a front face
                continue
            shade = 0.35 + 0.65 * abs(_dot(n, light))
            rgb = tuple(min(255, int(c * shade + 20)) for c in BASE)
            canvas.triangle([proj(v) for v in t], rgb)
        # panel frame and a 10 mm scale bar (bottom left)
        x0 = i * panel
        canvas.line(x0, 0, x0, panel - 1, (210, 205, 198))
        bar = 10 * scale
        if bar < panel - 2 * MARGIN:
            y = panel - 8
            canvas.line(int(x0 + 8), y, int(x0 + 8 + bar), y)
            canvas.line(int(x0 + 8), y - 3, int(x0 + 8), y + 3)
            canvas.line(int(x0 + 8 + bar), y - 3, int(x0 + 8 + bar), y + 3)
    return png_bytes(width, height, canvas.rows)


def box_triangles(x0: float, y0: float, z0: float, dx: float, dy: float, dz: float) -> list[Tri]:
    """The 12 triangles of an axis-aligned box (outward normals)."""
    x1, y1, z1 = x0 + dx, y0 + dy, z0 + dz
    v = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0), (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    faces = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (2, 3, 7, 6), (1, 2, 6, 5), (3, 0, 4, 7)]
    out: list[Tri] = []
    for a, b, c, d in faces:
        out += [(v[a], v[b], v[c]), (v[a], v[c], v[d])]
    return out
