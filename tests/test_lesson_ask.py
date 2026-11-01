"""S16 voice interruption: a question from the board about the current step (POST /lesson/ask).

Muse Spark (scripted chat) is given ONLY the verified lines up to the current step; its answer is held to
the lesson's action schema in code; every new math line goes through the same SymPy checks as a planned
lesson — a wrong line is dropped, never shown with a ✓ — and an unrelated question gets one brief
sentence and no actions. In rehearsal the scripted demo model gives a canned answer. No real API calls."""
import json
from types import SimpleNamespace as NS

import pytest

from lantern import demo, lesson as lesson_mod, lesson_ask, models
from lantern.core import Core
from test_lesson import quadratic
from test_routines import Both, Chat
from test_scheduler import Routed, config


def saved_lesson(tmp_path, core=None):
    """A planned-and-checked quadratic lesson, saved where the server serves lessons from."""
    out = lesson_mod.plan(NS(chat=Chat(lesson_plan=lambda kw: quadratic())), "solve x^2 - 5x + 6 = 0")
    folder = tmp_path / "lessons"
    name = lesson_mod.save(folder, out)
    return name, lesson_mod.load(folder, name)["lesson"]


def ask_chat(answer, seen=None):
    def handle(kw):
        if seen is not None:
            seen.append(kw)
        return answer(kw) if callable(answer) else answer
    return Chat(lesson_ask=handle)


GOOD = {"related": True, "spoken": "Minus two and minus three multiply to six and add to minus five.",
        "actions": [{"do": "highlight", "target": "eq1"},
                    {"do": "write_math", "id": "again", "latex": "x = 2 \\text{ or } x = 3", "x": 80, "y": 700,
                     "step": "solve each", "check": {"sympy": "[Eq(x, 2), Eq(x, 3)]",
                                                     "relation": "same_solutions", "of": "eq0"}}]}


def test_only_verified_lines_up_to_the_step_reach_the_model(tmp_path):
    _name, lesson = saved_lesson(tmp_path)
    seen = []
    out = lesson_ask.ask(NS(chat=ask_chat(GOOD, seen)), lesson, 3, "why minus two and minus three?")
    [req] = seen
    assert req["model"] == models.SPARK and req["response_format"]["json_schema"]["name"] == "lesson_ask"
    given = json.loads(req["messages"][-1]["content"])
    # step 3 = title, eq0, eq1 on the board: only eq0/eq1 may be told, never eq2/eq3 from later in the lesson
    assert [l["id"] for l in given["verified_lines_so_far"]] == ["eq0", "eq1"]
    assert "eq3" not in req["messages"][-1]["content"] and given["question"] == "why minus two and minus three?"
    assert out["related"] and out["step"] == 3 and out["spoken"].startswith("Minus two")
    highlight, again = out["actions"]
    assert highlight == {"do": "highlight", "target": "eq1"}
    assert again["verified"] is True and again["id"] == "ask-again"   # renamed, and ✓ only because SymPy agreed


def test_an_unverified_answer_line_is_dropped_never_shown(tmp_path):
    _name, lesson = saved_lesson(tmp_path)
    bad = {"related": True, "spoken": "Here is a shortcut.", "verified": True,   # the model may not set this
           "actions": [{"do": "write_math", "id": "fast", "latex": "x = 1 \\text{ or } x = 6", "x": 80, "y": 700,
                        "step": "solve", "verified": True,
                        "check": {"sympy": "[Eq(x, 1), Eq(x, 6)]", "relation": "same_solutions", "of": "eq0"}},
                       {"do": "erase", "target": "all"},
                       {"do": "write_math", "id": "mystery", "latex": "x = 7", "step": "trust me"}]}
    out = lesson_ask.ask(NS(chat=ask_chat(bad)), lesson, 6, "can I skip ahead?")
    assert out["actions"] == []
    dropped = {d["id"]: d["why"] for d in out["dropped"]}
    assert dropped["ask-fast"] == "its solutions differ from the line it comes from"
    assert "may not erase" in dropped["erase"] and "no check" in dropped["ask-mystery"]


def test_an_unrelated_question_gets_one_brief_sentence_and_no_actions(tmp_path):
    _name, lesson = saved_lesson(tmp_path)
    chat = ask_chat({"related": False, "spoken": "The weather in Delhi is sunny, and here is much more about it.",
                     "actions": [{"do": "highlight", "target": "eq1"}]})
    out = lesson_ask.ask(NS(chat=chat), lesson, 4, "what's the weather?")
    assert out == {"spoken": lesson_ask.UNRELATED, "related": False, "actions": [], "dropped": [], "step": 4}


def test_schema_refusals_and_bad_requests(tmp_path):
    _name, lesson = saved_lesson(tmp_path)
    with pytest.raises(lesson_ask.LessonAskError, match="expected shape"):
        lesson_ask.ask(NS(chat=ask_chat({"spoken": 42})), lesson, 2, "why?")
    with pytest.raises(lesson_ask.LessonAskError, match="didn't answer"):
        lesson_ask.ask(NS(chat=Chat()), lesson, 2, "why?")   # no lesson_ask handler: the chat double refuses
    with pytest.raises(lesson_ask.LessonAskError, match="out of range"):
        lesson_ask.ask(NS(chat=ask_chat(GOOD)), lesson, 99, "why?")
    with pytest.raises(lesson_ask.LessonAskError, match="ask a question"):
        lesson_ask.ask(NS(chat=ask_chat(GOOD)), lesson, 2, "   ")


def test_the_endpoint_answers_from_the_saved_lesson_and_logs_cost(tmp_path):
    chat = ask_chat(GOOD)
    core = Core(config(tmp_path), Both(Routed({}), chat), reminder_tick=0, workers=1)
    out = lesson_mod.plan(NS(chat=Chat(lesson_plan=lambda kw: quadratic())), "solve x^2 - 5x + 6 = 0")
    name = lesson_mod.save(core.lessons_folder(), out)
    from fastapi.testclient import TestClient
    from lantern.server import create_app
    http, h = TestClient(create_app(core, "t")), {"X-Lantern-Token": "t"}
    r = http.post("/lesson/ask", json={"name": name, "step": 3, "question": "why those numbers?"}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["related"] and body["actions"][1]["verified"] is True
    usage = core.memory.usage_by_model()
    assert usage and usage[0]["model"] == models.SPARK   # the call's cost is logged like other calls
    assert http.post("/lesson/ask", json={"name": name, "step": 3, "question": "why?" }).status_code == 401
    assert http.post("/lesson/ask", json={"name": "nope", "step": 0, "question": "why?"}, headers=h).status_code == 404
    assert http.post("/lesson/ask", json={"name": name, "step": 99, "question": "why?"}, headers=h).status_code == 409
    assert http.post("/lesson/ask", json={"name": name, "step": 1, "question": " "}, headers=h).status_code == 409
    core.shutdown()


def test_rehearsal_gives_a_canned_answer_and_a_canned_lesson(tmp_path):
    cfg = demo.prepare(tmp_path / "rehearsal")
    core, _hub, _phone = demo.build(cfg)
    try:
        out = lesson_mod.plan(core.client, "solve x^2 - 5x + 6 = 0")
        name = lesson_mod.save(core.lessons_folder(), out)
        from fastapi.testclient import TestClient
        from lantern.server import create_app
        http, h = TestClient(create_app(core, "t")), {"X-Lantern-Token": "t"}
        assert http.get("/health").json()["demo"] is True
        r = http.post("/lesson/ask", json={"name": name, "step": 3, "question": "why minus two and minus three?"},
                      headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["related"] and "SymPy checked it" in body["spoken"]
        assert body["actions"][0]["do"] == "highlight" and body["actions"][0]["target"] == "eq1"
        assert core.memory.total_cost() == 0.0   # rehearsal: the scripted model reports zero tokens
    finally:
        core.shutdown()
