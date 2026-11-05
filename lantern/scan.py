"""Photo to clean scan (S10): find the page with SAM 3.1, straighten it, and save a clean PNG and PDF.

  1. Find. SAM 3.1 (cookbook 07_sam, the same request as the camera check-in in sam.py) with one noun phrase per
     request, in CONCEPTS order, until one finds something. The largest mask wins. (Live, Oct 28: on a flat page
     on a plain desk SAM 3.1 found nothing for "document", "paper" or "letter" but found it as "sheet of paper".)
  2. Corners. From the mask's own pixels: the extreme points of x+y and x-y are the page's top-left,
     bottom-right, top-right and bottom-left corners.
  3. Straighten and clean, on this computer (Pillow): a perspective transform sized from the page's own edges,
     then greyscale and autocontrast, which is what makes a phone photo read like a scan.
  4. Save PNG + PDF into a writable zone through policy.py, named by date and what the page is
     ("2026-10-28 permit renewal notice.pdf"); an existing file is never overwritten.
No page found, or LANTERN_SCAN_SAM=off: the whole photo is cleaned the same way, and the result says so.
The photo goes to SAM 3.1 only because the user sent it to Lantern to be scanned; it is never sent anywhere else.
"""
from __future__ import annotations

import asyncio
import base64
import io
import math
import os
import re
import time
from pathlib import Path
from typing import Any

from . import models

CONCEPTS = ("document", "sheet of paper", "paper", "page", "letter")
MAX_PIXELS = 24_000_000          # a 6000 x 4000 phone photo
SAM_SIDE = 1600                  # the photo SAM sees is at most this wide or tall (corners scale back)
MIN_AREA = 0.05                  # a "page" smaller than 5% of the photo is not the page


class ScanError(ValueError):
    """The photo can't be scanned; the message is safe to show."""


# ---- 1. find the page --------------------------------------------------------------------------
async def _as_events(stream: Any):
    for event in stream:
        yield event.model_dump() if hasattr(event, "model_dump") else dict(event)


def masks(client: Any, image_url: str, concept: str) -> list[dict]:
    """One SAM 3.1 request; every mask found, as {"bounds": (l, t, r, b), "w", "h", "raster", "area"} in the
    coordinates of the image sent."""
    from meta_sam_parser import ResponsesStreamError, decode_mask_to_raster, image_segmentation_format, \
        parse_responses_stream
    stream = client.responses.create(model=models.SAM, stream=True, input=[{
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": concept}, {"type": "input_image", "image_url": image_url}]}])

    async def parse():
        return await parse_responses_stream(_as_events(stream), image_segmentation_format()).final_result()
    try:
        result = asyncio.run(parse())
    except ResponsesStreamError as e:
        raise ScanError(f"SAM couldn't finish: {str(e)[:160]}") from e
    out = []
    for r in result.records:
        if r.kind != "mask":
            continue
        raster = decode_mask_to_raster(r.mask)
        b = r.bounds
        scale = ((b.right - b.left) / r.mask.width) * ((b.bottom - b.top) / r.mask.height)
        out.append({"bounds": (b.left, b.top, b.right, b.bottom), "w": r.mask.width, "h": r.mask.height,
                    "raster": raster, "area": sum(raster) * scale})
    return out


def corners(mask: dict) -> list[tuple[float, float]]:
    """Top-left, top-right, bottom-right, bottom-left of the masked page, in image coordinates."""
    left, top, right, bottom = mask["bounds"]
    w, h, raster = mask["w"], mask["h"], mask["raster"]
    sx, sy = (right - left) / w, (bottom - top) / h
    best = {"tl": (math.inf, None), "br": (-math.inf, None), "tr": (-math.inf, None), "bl": (math.inf, None)}
    for y in range(h):
        row = y * w
        for x in range(w):
            if not raster[row + x]:
                continue
            px, py = left + (x + 0.5) * sx, top + (y + 0.5) * sy
            s, d = px + py, px - py
            if s < best["tl"][0]:
                best["tl"] = (s, (px, py))
            if s > best["br"][0]:
                best["br"] = (s, (px, py))
            if d > best["tr"][0]:
                best["tr"] = (d, (px, py))
            if d < best["bl"][0]:
                best["bl"] = (d, (px, py))
    if any(v[1] is None for v in best.values()):
        raise ScanError("the page mask is empty")
    return [best[k][1] for k in ("tl", "tr", "br", "bl")]


# ---- 2. straighten -------------------------------------------------------------------------------
def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting (8x8: no numpy needed)."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-12:
            raise ScanError("the page corners don't make a page")
        m[col], m[pivot] = m[pivot], m[col]
        for r in range(n):
            if r != col:
                f = m[r][col] / m[col][col]
                m[r] = [x - f * y for x, y in zip(m[r], m[col])]
    return [m[i][n] / m[i][i] for i in range(n)]


def perspective(quad: list[tuple[float, float]], width: int, height: int) -> list[float]:
    """Pillow's PERSPECTIVE coefficients: output (0,0)-(w,h) rectangle -> the quad in the photo."""
    dst = [(0, 0), (width, 0), (width, height), (0, height)]
    a, b = [], []
    for (x, y), (u, v) in zip(dst, quad):
        a.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        b.append(u)
        a.append([0, 0, 0, x, y, 1, -v * x, -v * y])
        b.append(v)
    return _solve(a, b)


def straighten(img, quad: list[tuple[float, float]]):
    from PIL import Image
    tl, tr, br, bl = quad
    width = int(max(math.dist(tl, tr), math.dist(bl, br)))
    height = int(max(math.dist(tl, bl), math.dist(tr, br)))
    if width < 50 or height < 50:
        raise ScanError("the page is too small in the photo")
    return img.transform((width, height), Image.Transform.PERSPECTIVE, perspective(quad, width, height),
                         Image.Resampling.BICUBIC)


def clean(img):
    from PIL import ImageOps
    return ImageOps.autocontrast(img.convert("L"), cutoff=1)


# ---- 3. the whole thing ------------------------------------------------------------------------------
def _data_url(img) -> tuple[str, tuple[int, int]]:
    from PIL import Image
    small = img.copy()
    small.thumbnail((SAM_SIDE, SAM_SIDE), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    small.convert("RGB").save(buf, "JPEG", quality=88)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode(), small.size


def file_name(kind: str = "", when: float | None = None) -> str:
    words = re.sub(r"[^A-Za-z0-9 ]+", " ", kind or "").split()[:6]
    return f"{time.strftime('%Y-%m-%d', time.localtime(when))} {' '.join(words).lower() or 'scan'}"


def scan(image: bytes, *, client: Any = None, policy=None, folder: str | Path, kind: str = "",
         memory=None, task_id: int | None = None, use_sam: bool | None = None) -> dict:
    """Photo bytes -> {"png", "pdf", "found", "corners", "concept", "sam_calls", "cost_usd", "size"}."""
    from PIL import Image, ImageOps, UnidentifiedImageError
    if use_sam is None:
        use_sam = os.environ.get("LANTERN_SCAN_SAM", "on").strip().lower() != "off"
    try:
        img = Image.open(io.BytesIO(image))
        if img.width * img.height > MAX_PIXELS:
            raise ScanError("that photo is too large to scan")
        img = ImageOps.exif_transpose(img).convert("RGB")
    except (UnidentifiedImageError, OSError) as e:
        raise ScanError("that isn't a photo I can open (send a JPEG or PNG)") from e
    found, quad, concept, calls = False, None, None, 0
    if use_sam and client is not None:
        url, (sw, sh) = _data_url(img)
        fx, fy = img.width / sw, img.height / sh
        for concept in CONCEPTS:
            calls += 1
            got = [m for m in masks(client, url, concept) if m["area"] >= MIN_AREA * sw * sh]
            if got:
                biggest = max(got, key=lambda m: m["area"])
                quad = [(x * fx, y * fy) for x, y in corners(biggest)]
                found = True
                break
        else:
            concept = None
    cost = models.sam_cost(calls) if calls else 0.0
    if memory is not None and calls:
        memory.log_usage(task_id, models.SAM, cost_usd=cost)
    page = clean(straighten(img, quad) if quad else img)
    stem = file_name(kind)
    png_path, pdf_path = _free(policy, Path(folder), stem)
    buf_png, buf_pdf = io.BytesIO(), io.BytesIO()
    page.save(buf_png, "PNG", optimize=True)
    page.save(buf_pdf, "PDF", resolution=150)
    policy.write_bytes(png_path, buf_png.getvalue())
    policy.write_bytes(pdf_path, buf_pdf.getvalue())
    return {"png": str(png_path), "pdf": str(pdf_path), "found": found, "concept": concept,
            "corners": [[round(x, 1), round(y, 1)] for x, y in quad] if quad else None, "sam_calls": calls,
            "cost_usd": cost, "size": list(page.size),
            "note": "" if found else ("I couldn't find the page's edges, so this is the whole photo, cleaned up"
                                      if use_sam else "the whole photo, cleaned up (SAM is off)")}


def _free(policy, folder: Path, stem: str) -> tuple[Path, Path]:
    for n in range(1, 100):
        name = stem if n == 1 else f"{stem} ({n})"
        png, pdf = folder / f"{name}.png", folder / f"{name}.pdf"
        if not policy.exists(png) and not policy.exists(pdf):
            return png, pdf
    raise ScanError("too many scans with that name today")
