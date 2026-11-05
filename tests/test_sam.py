"""M16: SAM 3.1 camera check-in, offline: a fake stream decoded by Meta's real parser."""
import base64
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("meta_sam_parser")
from meta_sam_parser._mask_codec import _encode_raster  # noqa: E402  (test fixtures only)

from fakes import FakeClient  # noqa: E402
from lantern import models  # noqa: E402
from lantern.audit import AuditLog, GuardedClient  # noqa: E402
from lantern.memory import Memory  # noqa: E402
from lantern.sam import CheckIn, CheckInError, check_concept  # noqa: E402

SNAP = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xe0 fake jpeg").decode()


def record(obj_id, left, top, right, bottom, w=640, h=480):
    mw, mh = right - left + 1, bottom - top + 1
    mask = _encode_raster([1] * (mw * mh), mw, mh)
    return f"{obj_id}<|box;x1={left};y1={top};x2={right};y2={bottom};w={w};h={h};c=0.93|><|mask;x=0;y=0;data={mh},{mw},{mask}|>"


def events(text, finish="response.completed"):
    lane = {"item_id": "msg_1", "output_index": 0, "content_index": 0}
    half = len(text) // 2
    out = [{"type": "response.created"},
           {"type": "response.output_text.delta", **lane, "delta": text[:half]},
           {"type": "response.output_text.delta", **lane, "delta": text[half:]},
           {"type": "response.content_part.done", **lane, "part": {"type": "output_text", "text": text}}]
    if finish:
        out.append({"type": finish, "response": {"status": finish.split(".")[1]}})
    return [NS(model_dump=lambda e=e: e) for e in out]


class StreamClient:
    def __init__(self, stream):
        self.stream, self.requests, self.responses = stream, [], self

    def create(self, **kw):
        self.requests.append(kw)
        return iter(self.stream)


@pytest.mark.parametrize("ok", ["coffee mug", "Water Glass", "plant", "cat", "rubber duck"])
def test_objects_are_fine(ok):
    assert check_concept(ok) == ok.lower()


@pytest.mark.parametrize("bad", ["face", "my face", "person", "woman", "smile", "emotion", "tired eyes", "body",
                                 "how happy am i", "", "x" * 50, "coffee; DROP TABLE"])
def test_people_faces_and_feelings_are_refused(bad):
    with pytest.raises(CheckInError):
        check_concept(bad)


def test_check_in_finds_objects(tmp_path):
    text = "<0f>" + record(0, 100, 120, 160, 200) + "," + record(1, 300, 140, 340, 210) + "\n"
    client = StreamClient(events(text))
    audit = AuditLog(tmp_path / "a.db")
    seen = []
    mem = Memory(tmp_path / "m.db")
    r = CheckIn(GuardedClient(client, audit), mem, seen.append).run(SNAP, "coffee mug")
    assert r["found"] == 2 and r["boxes"][0] == [100, 120, 161, 201] and r["reaction"] == "Ah, coffee. Good."
    assert r["stored"] is False and r["model"] == "SAM 3.1"
    req = client.requests[0]
    assert req["model"] == models.SAM and req["stream"] is True
    content = req["input"][0]["content"]
    assert content[0] == {"type": "input_text", "text": "coffee mug"} and content[1]["image_url"] == SNAP
    assert audit.summary()["cloud"]["by_model"][models.SAM]["calls"] == 1
    assert mem.usage_by_model()[0]["cost_usd"] == pytest.approx(models.sam_cost(1))
    assert [e["type"] for e in seen] == ["usage", "checkin"] and seen[1]["reaction"] == r["reaction"]
    assert not list(tmp_path.glob("*.jpg"))


def test_nothing_found_and_stream_problems(tmp_path):
    r = CheckIn(StreamClient(events("<0f>\n"))).run(SNAP, "plant")
    assert r["found"] == 0 and "plant" in r["reaction"]
    with pytest.raises(CheckInError, match="couldn't finish"):
        CheckIn(StreamClient(events("<0f>\n", finish="response.failed"))).run(SNAP, "plant")
    with pytest.raises(CheckInError, match="snapshot"):
        CheckIn(StreamClient([])).run("https://example.com/cam.jpg", "plant")


def test_checkin_route(tmp_path):
    from lantern.config import Config
    from lantern.core import Core
    from lantern.server import create_app
    cfg = Config(roots=[str(tmp_path)], db_path=str(tmp_path / "m.db"))
    cfg.audit_path, cfg.zones_file = str(tmp_path / "a.db"), str(tmp_path / "z.json")
    core = Core(cfg, FakeClient([]))
    core.checkin.client = StreamClient(events("<0f>" + record(0, 10, 10, 50, 50) + "\n"))
    http, h = TestClient(create_app(core, "t")), {"X-Lantern-Token": "t"}
    assert http.post("/checkin", json={"image": SNAP, "concept": "laptop"}).status_code == 401
    assert "coffee mug" in http.get("/checkin/ideas", headers=h).json()["ideas"]
    assert http.post("/checkin", json={"image": SNAP, "concept": "my face"}, headers=h).status_code == 400
    r = http.post("/checkin", json={"image": SNAP, "concept": "laptop"}, headers=h).json()
    assert r["found"] == 1 and r["reaction"].startswith("Found 1 laptop")
    core.shutdown()
