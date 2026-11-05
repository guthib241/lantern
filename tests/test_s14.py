"""S14 data and eval plumbing (scripted models only): the prompt is exactly the one compiler.match sends, the split
is by routine, rows the scrubber would touch are dropped, only answers the teacher agrees with are kept, the spend
cap stops the run, and the Modal eval reads a routine out of an answer."""
import json
import random
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import s14_export as s14  # noqa: E402
from lantern.routines.contract import skeleton_from_traces  # noqa: E402
from test_routines import TRACES, Both, Chat, mini_core, render  # noqa: E402
from test_scheduler import Routed  # noqa: E402


def fake_goals(catalog):
    return {r["name"]: {"same": [f"please do the {r['title'].lower()} thing, take {i}" for i in range(10)],
                        "near": [f"what is a {r['title'].lower()}, question {i}" for i in range(6)]}
            for r in catalog["routines"]}


def test_the_prompt_is_the_one_compiler_match_sends(tmp_path):
    chat = Chat(routine_match=lambda kw: {"routine": "city-permit", "params": []})
    core, page = mini_core(tmp_path, Routed({}), chat)
    c = skeleton_from_traces("fill the city permit application", TRACES, {}, {})
    c["name"], c["params"] = "city-permit", []
    core.compiler.store.save(c, render(c), {"ok": True}, how="test", trusted=True)
    core.compiler.match("[whatsapp] submit my permit paperwork to the city", client=Both(None, chat))
    [req] = chat.of("routine_match")
    r = core.compiler.store.load("city-permit")
    listing = [s14.entry({"name": r.name, "title": r.meta.get("title"), "examples": r.meta.get("examples") or []})]
    assert req["messages"] == s14.messages("[whatsapp] submit my permit paperwork to the city", listing)
    core.shutdown()


def test_the_split_is_by_routine_and_every_kind_of_goal_is_there():
    catalog = s14.load_catalog()
    held = set(catalog["held_out"])
    examples = s14.build(catalog, fake_goals(catalog), random.Random(s14.SEED))
    train = [e for e in examples if e["split"] == "train"]
    heldout = [e for e in examples if e["split"] == "heldout"]
    assert train and heldout and {e["source"] for e in heldout} <= held
    assert not any(x["name"] in held for e in train for x in e["listing"])        # unseen in training
    assert all(len(e["listing"]) in (s14.DISTRACTORS, s14.DISTRACTORS + 1) for e in examples)
    for e in examples:                                                           # the label is in the list, or none
        names = [x["name"] for x in e["listing"]]
        assert e["label"] == "none" or e["label"] in names
        if e["label"] != "none" and e["goal"].startswith("please"):
            assert e["source"] in names
    absent = [e for e in examples if e["goal"].startswith("please") and e["label"] == "none"]
    assert absent and all(e["source"] not in [x["name"] for x in e["listing"]] for e in absent)
    assert ("Tidy my Downloads folder.", "tidy-downloads") in {(e["goal"], e["label"]) for e in examples}
    again = s14.build(catalog, fake_goals(catalog), random.Random(s14.SEED))
    assert [e["listing"] for e in again] == [e["listing"] for e in examples]      # same seed, same data


def test_unsafe_rows_are_dropped():
    assert s14.clean("renew my library books")
    assert not s14.clean("pay the bill with card 4111 1111 1111 1111")
    assert not s14.clean("move /home/asha/Downloads/report.pdf")
    assert not s14.clean(r"open C:\Users\asha\Desktop")


class Api:
    """chat.completions: Spark writes goals; Wonder (the teacher) answers; one answer disagrees."""

    def __init__(self, catalog, wrong_goal: str):
        self.catalog, self.wrong, self.requests = catalog, wrong_goal, []
        self.chat = NS(completions=self)

    def create(self, **kw):
        self.requests.append(kw)
        name = kw["response_format"]["json_schema"]["name"]
        if name == "s14_goals":
            r = json.loads(kw["messages"][1]["content"])
            data = fake_goals({"routines": [r]})[r["name"]]
        else:
            ask = json.loads(kw["messages"][1]["content"])
            names = [x["name"] for x in ask["routines"]]
            pick = next((n for n in names if n.replace("-", " ") in ask["request"]
                         or self._title(n).lower() in ask["request"]), "none")
            if ask["request"].startswith("what is"):
                pick = "none"
            data = {"routine": names[0] if ask["request"] == self.wrong else pick, "params": []}
        usage = NS(prompt_tokens=600, completion_tokens=100, prompt_tokens_details=NS(cached_tokens=0),
                   completion_tokens_details=NS(reasoning_tokens=80))
        return NS(choices=[NS(message=NS(content=json.dumps(data)))], usage=usage)

    def _title(self, name):
        return next(r["title"] for r in self.catalog["routines"] if r["name"] == name)


def test_the_export_keeps_what_the_teacher_agrees_with_and_stops_at_the_cap(tmp_path, monkeypatch):
    catalog = s14.load_catalog()
    wrong = "please do the sign up for a webinar thing, take 0"
    api = Api(catalog, wrong)
    monkeypatch.setenv("MODEL_API_KEY", "test")
    monkeypatch.setattr("openai.OpenAI", lambda **kw: api)
    assert s14.main(["--out", str(tmp_path), "--max-usd", "50", "--spark-ref"]) == 0
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["candidates"] == len(catalog["routines"]) * 16 + len(s14.STORY)
    assert {"goal": wrong, "label": "webinar-signup", "teacher": report["disagreements"][0]["teacher"]} in \
        report["disagreements"]
    rows = [json.loads(x) for x in (tmp_path / "heldout.jsonl").read_text().splitlines()]
    assert rows and all(r["messages"][2] == {"role": "assistant", "content": s14.answer(r["expected"])} for r in rows)
    assert wrong not in (tmp_path / "heldout.jsonl").read_text()                  # disagreement dropped
    assert report["heldout"]["examples"] == len(rows) and report["spark_heldout"]["of"] == len(rows)
    models_used = {kw["model"] for kw in api.requests}
    assert models_used == {"muse-spark-1.3", "muse-wonder-1.0"}                  # never a -contributor model
    calls = len(api.requests)
    assert s14.main(["--out", str(tmp_path)]) == 0 and len(api.requests) == calls  # cached: no new calls
    with pytest.raises(SystemExit, match="cap"):
        s14.main(["--out", str(tmp_path / "again"), "--max-usd", "0.01"])


def test_the_modal_eval_reads_the_routine_out_of_an_answer():
    modal = pytest.importorskip("modal")
    if not hasattr(modal, "App"):   # a folder called modal/ on the path is not Modal's SDK: skip, don't crash
        pytest.skip("Modal's SDK isn't installed")
    modal_script = pytest.importorskip("s14_modal")
    assert modal_script.routine_of('thinking... {"routine": "x"} then {"routine": "water-bill", "params": []}') == \
        "water-bill"
    assert modal_script.routine_of("no json here") == "?"
    assert modal_script.routine_of('{"routine": ""}') == "?"
