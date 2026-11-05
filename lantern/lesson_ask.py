"""Interrupt a lesson with a question (S16): Muse Spark answers about the current step, on the board.

The model is given ONLY the lesson's verified math lines up to the step the board has reached, plus the
learner's question (their own words, from Muse Voice Transcribe or typed). It answers with structured
output (Chat Completions, the same shape `lesson.plan` uses): a short spoken line and up to three board
actions in the lesson's own action schema. Code holds that answer to the schema (`lesson.shape`), runs
every new math line through the same SymPy checks as a planned lesson (`mathcheck.verify` over the
verified lines plus the answer's lines, in order), and drops anything unverified — a dropped line is
never shown, and nothing here can set `verified`: only a passed check does. Actions that point at
something not on the board at the pause, erase lesson lines, or pause again are dropped too, so the
lesson can always resume from where it stopped. An unrelated question gets one brief sentence and no
actions, whatever the model sent.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from . import lesson as lesson_mod
from . import mathcheck

MAX_QUESTION = 500
MAX_SPOKEN = 600
MAX_ACTIONS = 3
UNRELATED = "That isn't about this part of the lesson, so let's stay with the step on the board."

ASK_RULES = """You answer a learner's question in the middle of a lesson on Lantern's teaching board.
You are given only the lesson's verified math lines so far (each checked by SymPy) and the question.
Answer with only the JSON object the schema asks for:
- related: true only if the question is about those lines or the step they lead to.
- spoken: if related, a short answer (at most three sentences) a tutor would say aloud, using only what
  those lines say; if not related, one brief sentence saying so.
- actions: optionally, up to 3 board actions (the lesson's schema) that help show the answer, e.g. a
  highlight of a line already on the board (target: its id), or a new write_math line. A new math line
  that follows from an earlier one needs a step and a check, exactly as in a lesson (SymPy syntax; `of`
  is the id of a verified line given to you, or of a new line you wrote just before it). Give new items
  ids starting with "ask" (ask1, ask2). Place new lines at x 80, y 640 or lower. Never erase, never pause.
- Never claim a line is verified: Lantern checks every new math line with SymPy and drops the ones that
  don't check out.
The question is the learner's own words about their lesson; answer it, and nothing else it may ask for."""

ASK_SCHEMA = {
    "type": "object",
    "properties": {
        "related": {"type": "boolean"},
        "spoken": {"type": "string"},
        "actions": {"type": "array", "maxItems": MAX_ACTIONS,
                    "items": lesson_mod.PLAN_SCHEMA["properties"]["actions"]["items"]},
    },
    "required": ["related", "spoken"],
}


class LessonAskError(ValueError):
    """No answer could be made; the message is safe to show."""


def context_lines(lesson: dict, step: int) -> list[dict]:
    """The verified math lines among the first `step` actions: all the model may be told about."""
    out = []
    for a in lesson.get("actions", [])[:step]:
        if a.get("do") == "write_math" and a.get("verified") is True and isinstance(a.get("check"), dict):
            out.append({"id": a.get("id"), "latex": a.get("latex"), "step": a.get("step"), "check": a["check"]})
    return out


def _live_at(lesson: dict, step: int) -> dict[str, dict]:
    """id -> action for what is on the board after the first `step` actions (erases applied)."""
    live: dict[str, dict] = {}
    for a in lesson.get("actions", [])[:step]:
        if a.get("do") == "erase":
            for t in (list(live) if a.get("target") == "all" else lesson_mod._targets(a.get("target"))):
                live.pop(t, None)
        elif a.get("id"):
            live[a["id"]] = a
    return live


def _rename(actions: list[dict]) -> dict[str, str]:
    """New items get ask- ids, so they can never shadow a lesson line still to come; references follow."""
    own = {a["id"] for a in actions if a.get("id")}
    ren = {i: i if i.startswith("ask") else f"ask-{i}" for i in own}

    def ref(v: Any) -> Any:
        return ren.get(v, v) if isinstance(v, str) else v

    for a in actions:
        if a.get("id") in ren:
            a["id"] = ren[a["id"]]
        for k in ("target", "around", "axes"):
            if k in a:
                a[k] = ref(a[k])
        if isinstance(a.get("target"), list):
            a["target"] = [ref(t) for t in a["target"]]
        if a.get("check", {}).get("of"):
            a["check"] = {**a["check"], "of": ref(a["check"]["of"])}
    return ren


def clean_answer(data: Any, lesson: dict, step: int) -> dict:
    """A model answer held to the board's rules: {"spoken", "related", "actions", "dropped"}.

    Raises LessonAskError when the answer isn't even the right shape (schema refusal)."""
    if not isinstance(data, dict) or not isinstance(data.get("related"), bool) \
            or not isinstance(data.get("spoken"), str):
        raise LessonAskError("the answer didn't come back in the expected shape")
    spoken = " ".join(data["spoken"].split())[:MAX_SPOKEN]
    if not data["related"]:
        return {"spoken": UNRELATED, "related": False, "actions": [], "dropped": []}
    if not spoken:
        raise LessonAskError("the answer was empty")
    raw = data.get("actions") or []
    if not isinstance(raw, list):
        raise LessonAskError("the answer's actions weren't a list")
    dropped: list[dict] = [{"id": "actions", "why": "only the first 3 actions are shown"}] if len(raw) > MAX_ACTIONS else []
    shaped, problems = lesson_mod.shape({"title": lesson.get("title", "A lesson"),
                                         "subject": lesson.get("subject", "other"),
                                         "actions": raw[:MAX_ACTIONS]})
    actions = shaped["actions"]
    dropped += [{"id": f"action {i + 1}", "why": p.split(": ", 1)[-1]} for i, p in enumerate(problems)]
    _rename(actions)
    live = _live_at(lesson, step)
    axes = {i: a for i, a in live.items() if a.get("shape") == "axes"}
    combined = [a for a in lesson.get("actions", [])[:step]
                if a.get("do") == "write_math" and a.get("verified") is True] + actions
    results = mathcheck.verify({"actions": combined})
    kept: list[dict] = []
    on_board = dict(live)   # ids an answer action may point at: the board at the pause, then its own items
    for a in actions:
        aid = a.get("id") or a["do"]
        why = ""
        if a["do"] == "pause":
            why = "the lesson is already paused for the question"
        elif a["do"] == "erase":
            why = "an answer may not erase the lesson"
        elif aid in live:
            why = f"the id {aid!r} is already on the board"
        elif a["do"] == "highlight" and a["target"] not in on_board:
            why = f"it points at {a['target']!r}, which isn't on the board"
        elif a.get("shape") == "circle" and a.get("around") and a["around"] not in on_board:
            why = f"it circles {a['around']!r}, which isn't on the board"
        elif a.get("shape") == "plot":
            ax = axes.get(a.get("axes", ""))
            if ax is None:
                why = "its axes aren't on the board"
            else:
                try:
                    a = {**a, "points": lesson_mod.plot_points(a["fn"], ax["xrange"], ax["yrange"])}
                    del a["fn"]
                except mathcheck.MathError as e:
                    why = f"its fn can't be plotted: {e}"
        elif a["do"] == "write_math":
            if "check" in a:
                ok, reason = results.get(aid, (False, "it wasn't checked"))
                if not ok:
                    why = reason
                a["verified"] = ok
            elif lesson.get("subject") in lesson_mod.CHECKED and a.get("step"):
                why = "it's a step with no check, and every step in algebra or calculus must have one"
            else:
                a["verified"] = False
        if why:
            dropped.append({"id": aid, "why": why})
            continue
        if a.get("shape") == "axes":
            axes[a["id"]] = a
        if a.get("id"):
            on_board[a["id"]] = a
        kept.append(a)
    return {"spoken": spoken, "related": True, "actions": kept, "dropped": dropped}


def ask(client: Any, lesson: dict, step: int, question: str, *, memory=None,
        emit: Callable[[dict], None] | None = None) -> dict:
    """Answer `question` about `lesson` at `step`. Only the verified lines up to `step` reach the model,
    and the audited client logs the call's cost like every other call (reader.ask_json)."""
    from .ladder import RUNGS
    from .reader import ask_json
    if not isinstance(lesson, dict) or not isinstance(lesson.get("actions"), list) or not lesson["actions"]:
        raise LessonAskError("that lesson has nothing to ask about")
    if isinstance(step, bool) or not isinstance(step, int) or not 0 <= step <= len(lesson["actions"]):
        raise LessonAskError("the board's step is out of range for this lesson")
    question = " ".join(str(question or "").split())[:MAX_QUESTION]
    if not question:
        raise LessonAskError("ask a question first")
    lines = context_lines(lesson, step)
    user = json.dumps({"lesson": lesson.get("title", ""), "subject": lesson.get("subject", "other"),
                       "verified_lines_so_far": lines, "question": question}, ensure_ascii=False)
    data, _cost, error = ask_json(client, memory=memory, task_id=None, rung=RUNGS[3], system=ASK_RULES,
                                  user=user, schema=ASK_SCHEMA, name="lesson_ask", kind="teach", emit=emit)
    if data is None:
        raise LessonAskError(f"Muse Spark didn't answer: {error}")
    out = clean_answer(data, lesson, step)
    out["step"] = step
    return out
