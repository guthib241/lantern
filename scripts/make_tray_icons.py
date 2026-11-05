"""Draw Lantern's menu bar / tray icon as PNGs, with the standard library only.

    python scripts/make_tray_icons.py      -> desktop/assets/tray/

The glyph is the same as the app icon (a handle, a rounded body, a flame), drawn as anti-aliased
strokes by supersampling. macOS gets black "Template" images it tints for light/dark menu bars;
Windows and Linux get an amber version that reads on dark and light taskbars.
"""
from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "desktop" / "assets" / "tray"
STROKE = 1.9          # in the 24-unit design grid
SAMPLES = 4           # supersampling per axis


def seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def rrect_dist(px, py, x0, y0, x1, y1, r):
    """Distance to the outline of a rounded rectangle."""
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    hx, hy = (x1 - x0) / 2 - r, (y1 - y0) / 2 - r
    qx, qy = abs(px - cx) - hx, abs(py - cy) - hy
    outside = math.hypot(max(qx, 0), max(qy, 0)) + min(max(qx, qy), 0) - r
    return abs(outside)


def flame(px, py):
    """Inside the flame: a circle with a point on top."""
    if math.hypot(px - 12, py - 13.8) <= 1.95:
        return True
    if 10.2 <= py <= 13.8:   # the tip: a triangle from (12, 10.2) to the circle's sides
        half = (py - 10.2) / (13.8 - 10.2) * 1.95
        return abs(px - 12) <= half
    return False


def ink(px, py) -> bool:
    w = STROKE / 2
    if 0 <= math.atan2(-(py - 4.7), px - 12) <= math.pi and abs(math.hypot(px - 12, py - 4.7) - 2.2) <= w:
        return True   # the handle (upper half circle)
    if seg_dist(px, py, 8.2, 6.6, 15.8, 6.6) <= w or seg_dist(px, py, 9.2, 20.4, 14.8, 20.4) <= w:
        return True   # caps
    if rrect_dist(px, py, 6.8, 6.6, 17.2, 20.4, 4.4) <= w:
        return True   # the body
    return flame(px, py)


def render(size: int, rgb: tuple[int, int, int]) -> bytes:
    rows = []
    scale = 24 / size
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            hits = sum(ink((x + (i + .5) / SAMPLES) * scale, (y + (j + .5) / SAMPLES) * scale)
                       for i in range(SAMPLES) for j in range(SAMPLES))
            row += bytes([*rgb, round(255 * hits / SAMPLES ** 2)])
        rows.append(bytes(row))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)   # 8-bit RGBA
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
            + chunk(b"IEND", b""))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, rgb in (("lanternTemplate", (0, 0, 0)), ("lantern", (255, 159, 10))):
        (OUT / f"{name}.png").write_bytes(render(16, rgb))
        (OUT / f"{name}@2x.png").write_bytes(render(32, rgb))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
