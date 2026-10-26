"""M1: model registry, cost table, per-task usage logging."""
import json

import pytest

from fakes import FakeClient, call, usage
from lantern import models
from lantern.agent import Agent
from lantern.memory import Memory
from lantern.tools import Toolbox
from lantern.voice import TranscribeError, build_multipart, transcribe


def test_ids_live_in_one_place():
    assert models.SPARK == "muse-spark-1.3"
    assert models.WONDER == "muse-wonder-1.0"
    assert models.IMAGE == "muse-image-1.0"
    assert models.VOICE == "muse-voice-transcribe-1.0"
    assert models.SAM == "sam-3.1"
    assert models.BASE_URL == "https://api.meta.ai/v1"


def test_contributor_models_refused():
    with pytest.raises(models.ModelPolicyError):
        models.check_model("muse-spark-1.3-contributor")
    assert models.check_model(models.SPARK) == models.SPARK


def test_agent_refuses_contributor_model(tmp_path):
    mem = Memory(tmp_path / "m.db")
    tb = Toolbox([tmp_path], mem, lambda d: True)
    with pytest.raises(models.ModelPolicyError):
        Agent(FakeClient([]), mem, tb, model="muse-spark-1.2-contributor")


def test_token_cost_uses_cached_rate():
    u = models.Usage(input_tokens=1_000_000, cached_tokens=400_000, output_tokens=100_000)
    # 600k fresh * 1.25 + 400k cached * 0.15 + 100k out * 4.25, per 1M
    assert models.token_cost(models.SPARK, u) == pytest.approx(0.75 + 0.06 + 0.425)
    assert models.token_cost(models.WONDER, models.Usage(output_tokens=1_000_000)) == pytest.approx(20.0)
    assert models.token_cost("unknown-model", u) == 0.0


def test_usage_parses_sdk_objects_dicts_and_none():
    assert models.Usage.from_response(usage(10, 4, 3)) == models.Usage(10, 4, 3, 0)
    d = {"input_tokens": 5, "input_tokens_details": {"cached_tokens": 2}, "output_tokens": 1}
    assert models.Usage.from_response(d).cached_tokens == 2
    assert models.Usage.from_response(None) == models.Usage()


def test_other_prices():
    assert models.image_cost(3) == pytest.approx(0.03)
    assert models.audio_cost(3_600_999) == pytest.approx(0.18)  # whole seconds, rounded down
    assert models.sam_cost(1000) == pytest.approx(2.5)


def test_agent_logs_tokens_and_cost_per_task(tmp_path):
    mem = Memory(tmp_path / "m.db")
    tb = Toolbox([tmp_path], mem, lambda d: True)
    events = []
    client = FakeClient([
        ([call("list_dir", "c1")], "", usage(1000, 0, 100)),
        ([call("finish", "c2", status="done", summary="listed")], "", usage(2000, 1000, 50)),
        ([], "There.", usage(2100, 2000, 10)),
    ])
    r = Agent(client, mem, tb, on_event=lambda k, d: events.append((k, d))).run("list my files")
    u = mem.task_usage(r.task_id)
    assert u["calls"] == 3 and u["input_tokens"] == 5100 and u["cached_tokens"] == 3000
    expected = sum(models.token_cost(models.SPARK, models.Usage(i, c, o))
                   for i, c, o in [(1000, 0, 100), (2000, 1000, 50), (2100, 2000, 10)])
    assert r.cost_usd == pytest.approx(expected) and mem.total_cost() == pytest.approx(expected)
    badges = [d["badge"] for k, d in events if k == "usage"]
    assert badges == ["Muse Spark"] * 3
    assert mem.usage_by_model()[0]["model"] == models.SPARK


def test_missing_usage_is_zero_cost(tmp_path):
    mem = Memory(tmp_path / "m.db")
    tb = Toolbox([tmp_path], mem, lambda d: True)
    r = Agent(FakeClient([([], "hi")]), mem, tb).run("hi")
    assert r.cost_usd == 0 and mem.task_usage(r.task_id)["calls"] == 1


# ---- voice client request shape (no network) --------------------------------
def test_transcribe_builds_documented_request(tmp_path):
    wav = tmp_path / "note.wav"
    wav.write_bytes(b"RIFFfake")
    seen = {}

    def post(url, body, headers):
        seen.update(url=url, body=body, headers=headers)
        return 200, json.dumps({"sessionId": "s1", "transcript": "hello lantern", "audioDurationMs": 900}).encode()

    out = transcribe(wav, api_key="k", keywords=["Priya"], post=post)
    assert out["transcript"] == "hello lantern"
    assert seen["url"] == "https://api.meta.ai/v1/asr/transcribe"
    assert seen["headers"]["Authorization"] == "Bearer k"
    assert b'name="request"' in seen["body"] and b'name="audio"; filename="note.wav"' in seen["body"]
    assert b'"audioEncoding": "WAV"' in seen["body"] and b'"keywords": ["Priya"]' in seen["body"]
    assert b'"model": "muse-voice-transcribe-1.0"' in seen["body"]


def test_transcribe_errors(tmp_path):
    wav = tmp_path / "n.wav"
    wav.write_bytes(b"x")
    with pytest.raises(TranscribeError):
        transcribe(wav, api_key="k", post=lambda u, b, h: (401, b"bad key"))
    with pytest.raises(ValueError):
        transcribe(wav, api_key="k", mode="SING")


def test_multipart_has_closing_boundary():
    body, ctype = build_multipart({"a": 1}, b"AUDIO", "x.wav")
    boundary = ctype.split("boundary=")[1]
    assert body.endswith(f"--{boundary}--\r\n".encode())


def test_smoke_skips_without_key(monkeypatch, capsys):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("smoke", Path(__file__).parent.parent / "scripts" / "smoke.py")
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("LANTERN_GLIMMER_URL", raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None, raising=False)
    assert smoke.main([]) == 0
    out = capsys.readouterr().out
    assert out.count("SKIP") == len(smoke.CHECKS) == 7
    assert smoke.tiny_png().startswith(b"\x89PNG")
