"""S10: photo to clean scan. A synthetic phone photo (a tilted white page on a dark desk) and a SAM 3.1 mask of
that page in the real wire format, decoded by Meta's own parser; the page is found, straightened, cleaned and
saved as PNG + PDF in a writable zone through policy.py."""
import io
import json

import pytest

pytest.importorskip("meta_sam_parser")
PIL = pytest.importorskip("PIL")
from meta_sam_parser._mask_codec import _encode_raster  # noqa: E402  (test fixtures only)
from PIL import Image, ImageDraw  # noqa: E402

from lantern import models  # noqa: E402
from lantern.memory import Memory  # noqa: E402
from lantern.policy import PRIVATE, WRITABLE, Policy, PolicyError, Rule  # noqa: E402
from lantern.scan import ScanError, corners, file_name, perspective, scan  # noqa: E402
from test_sam import StreamClient, events  # noqa: E402

W, H = 600, 450
PAGE = [(125, 75), (475, 100), (450, 400), (100, 375)]      # tl, tr, br, bl: a page photographed at an angle


def photo() -> bytes:
    img = Image.new("RGB", (W, H), (38, 36, 40))
    d = ImageDraw.Draw(img)
    d.polygon(PAGE, fill=(235, 233, 228))
    for i in range(6):                                       # lines of "text" across the page
        y = 130 + i * 40
        d.line([(160, y + 3), (420, y + 10)], fill=(30, 30, 30), width=4)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=92)
    return buf.getvalue()


def inside(px, py, quad):
    sign = None
    for (x1, y1), (x2, y2) in zip(quad, quad[1:] + quad[:1]):
        c = (x2 - x1) * (py - y1) - (y2 - y1) * (px - x1)
        if c != 0:
            if sign is None:
                sign = c > 0
            elif (c > 0) != sign:
                return False
    return True


def page_record(quad=PAGE, obj="0"):
    left, top = min(x for x, _ in quad), min(y for _, y in quad)
    right, bottom = max(x for x, _ in quad), max(y for _, y in quad)
    mw, mh = right - left + 1, bottom - top + 1
    raster = [1 if inside(left + x + 0.5, top + y + 0.5, quad) else 0 for y in range(mh) for x in range(mw)]
    mask = _encode_raster(raster, mw, mh)
    return (f"{obj}<|box;x1={left};y1={top};x2={right};y2={bottom};w={W};h={H};c=0.95|>"
            f"<|mask;x=0;y=0;data={mh},{mw},{mask}|>")


class SamByConcept(StreamClient):
    """A different stream per noun phrase; empty for the ones that find nothing."""

    def __init__(self, by_concept: dict):
        super().__init__([])
        self.by_concept = by_concept

    def create(self, **kw):
        self.requests.append(kw)
        concept = kw["input"][0]["content"][0]["text"]
        return iter(events(self.by_concept.get(concept, "<0f>\n")))


@pytest.fixture
def policy(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "Private").mkdir()
    return Policy([Rule.make(tmp_path / "docs", WRITABLE), Rule.make(tmp_path / "Private", PRIVATE)])


def test_the_page_is_found_straightened_and_saved(tmp_path, policy):
    client = SamByConcept({"document": "<0f>" + page_record() + "\n"})
    mem = Memory(str(tmp_path / "m.db"))
    out = scan(photo(), client=client, policy=policy, folder=tmp_path / "docs" / "Scans",
               kind="Permit renewal notice!", memory=mem, task_id=None)
    assert out["found"] and out["concept"] == "document" and out["sam_calls"] == 1
    for (x, y), (ex, ey) in zip(out["corners"], PAGE):           # within a few pixels of the real corners
        assert abs(x - ex) <= 4 and abs(y - ey) <= 4, out["corners"]
    assert out["png"].endswith("permit renewal notice.png") and out["pdf"].endswith("permit renewal notice.pdf")
    page = Image.open(out["png"])
    assert page.mode == "L" and 330 <= page.width <= 370 and 290 <= page.height <= 330    # the page's own size
    for x, y in [(4, 4), (page.width - 5, 4), (4, page.height - 5), (page.width - 5, page.height - 5)]:
        assert page.getpixel((x, y)) > 200, (x, y)               # page to the edges: no desk left in the corners
    assert open(out["pdf"], "rb").read(5) == b"%PDF-"
    req = client.requests[0]
    assert req["model"] == models.SAM and req["stream"] is True
    assert req["input"][0]["content"][1]["image_url"].startswith("data:image/jpeg;base64,")
    assert mem.total_cost() == pytest.approx(models.sam_cost(1))    # logged like every model call


def test_no_page_found_means_the_whole_photo_cleaned_and_saying_so(tmp_path, policy):
    client = SamByConcept({})
    out = scan(photo(), client=client, policy=policy, folder=tmp_path / "docs" / "Scans")
    assert not out["found"] and out["sam_calls"] == 3 and out["size"] == [W, H]
    assert [r["input"][0]["content"][0]["text"] for r in client.requests] == ["document", "paper", "letter"]
    assert "couldn't find the page" in out["note"]
    again = scan(photo(), client=client, policy=policy, folder=tmp_path / "docs" / "Scans", use_sam=False)
    assert again["sam_calls"] == 0 and again["png"].endswith("scan (2).png")   # never overwrites the first
    assert file_name("", 0).endswith(" scan")


def test_a_small_blob_is_not_the_page_and_bad_input_is_refused(tmp_path, policy):
    tiny = [(10, 10), (30, 10), (30, 30), (10, 30)]
    client = SamByConcept({"document": "<0f>" + page_record(tiny) + "\n", "paper": "<0f>" + page_record() + "\n"})
    out = scan(photo(), client=client, policy=policy, folder=tmp_path / "docs" / "Scans")
    assert out["concept"] == "paper"                             # the stamp-sized "document" was skipped
    with pytest.raises(ScanError, match="photo I can open"):
        scan(b"not a photo", client=client, policy=policy, folder=tmp_path / "docs" / "Scans")
    with pytest.raises(PolicyError):                             # scans are written through policy.py only
        scan(photo(), policy=policy, folder=tmp_path / "Private", use_sam=False)


def test_corners_and_perspective_math():
    quad = [(10.0, 20.0), (110.0, 25.0), (105.0, 120.0), (5.0, 115.0)]
    a, b, c, d, e, f, g, h = perspective(quad, 100, 100)
    for (x, y), (u, v) in zip([(0, 0), (100, 0), (100, 100), (0, 100)], quad):
        den = g * x + h * y + 1
        assert abs((a * x + b * y + c) / den - u) < 1e-6 and abs((d * x + e * y + f) / den - v) < 1e-6
    raster = [1] * 12
    assert corners({"bounds": (0, 0, 4, 3), "w": 4, "h": 3, "raster": raster}) == [(0.5, 0.5), (3.5, 0.5), (3.5, 2.5), (0.5, 2.5)]
    assert json.dumps(file_name("Water bill / Oct", 0)).count("/") == 0
