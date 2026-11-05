"""S16 teaching board: the sample lessons validate against the JSON schema, every ✓ is re-verified with SymPy,
bad lessons are refused, and the page plays both lessons to the end in a real browser (muted, fast)."""
import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import parallel_support as ps  # noqa: E402

browser = ps.browser   # the Playwright fixture

jsonschema = pytest.importorskip("jsonschema")
SCHEMA = json.loads((ps.BOARD / "schema/lesson.schema.json").read_text(encoding="utf-8"))
INDEX = json.loads((ps.BOARD / "lessons/index.json").read_text(encoding="utf-8"))
LESSONS = {e["file"]: json.loads((ps.BOARD / "lessons" / e["file"]).read_text(encoding="utf-8")) for e in INDEX["lessons"]}
VALIDATOR = jsonschema.Draft202012Validator(SCHEMA)


def test_schema_is_valid_and_lessons_match_it():
    jsonschema.Draft202012Validator.check_schema(SCHEMA)
    assert set(LESSONS) == {p.name for p in (ps.BOARD / "lessons").glob("*.json")} - {"index.json"}
    assert len(LESSONS) >= 2
    for name, lesson in LESSONS.items():
        errors = [f"{list(e.path)}: {e.message}" for e in VALIDATOR.iter_errors(lesson)]
        assert errors == [], name
        assert [e["title"] for e in INDEX["lessons"] if e["file"] == name] == [lesson["title"]]
        kinds = {a["do"] for a in lesson["actions"]}
        assert {"write_math", "write_text", "draw", "highlight", "pause"} <= kinds, name
        assert all(a.get("say") for a in lesson["actions"] if a["do"] in ("write_math", "highlight")), name


def test_every_action_kind_and_shape_is_exercised_by_the_samples():
    actions = [a for lesson in LESSONS.values() for a in lesson["actions"]]
    assert {a["do"] for a in actions} == {"write_math", "write_text", "draw", "highlight", "erase", "pause"}
    assert {a["shape"] for a in actions if a["do"] == "draw"} == {"axes", "plot", "circle", "arrow"} | (
        {"line"} if any(a.get("shape") == "line" for a in actions) else set())


BAD = [
    ("missing latex", {"do": "write_math"}),
    ("unknown action", {"do": "teleport"}),
    ("unknown shape", {"do": "draw", "shape": "star"}),
    ("line without end", {"do": "draw", "shape": "line", "from": [0, 0]}),
    ("off the board", {"do": "write_text", "text": "hi", "x": 5000}),
    ("plot without axes", {"do": "draw", "shape": "plot", "points": [[0, 0], [1, 1]]}),
    ("circle with neither center nor around", {"do": "draw", "shape": "circle"}),
    ("check without of", {"do": "write_math", "latex": "x", "check": {"sympy": "x", "relation": "equal"}}),
    ("pause without seconds", {"do": "pause"}),
    ("extra field", {"do": "write_text", "text": "hi", "onclick": "alert(1)"}),
    ("bad colour", {"do": "write_text", "text": "hi", "color": "url(javascript:1)"}),
]


@pytest.mark.parametrize("why, action", BAD, ids=[b[0] for b in BAD])
def test_schema_refuses_bad_actions(why, action):
    assert not VALIDATOR.is_valid({"version": 1, "title": "t", "actions": [action]}), why


def test_references_point_at_items_on_the_board():
    """What a schema can't say: highlight/erase/circle/plot/check targets must be on the board at that point."""
    for name, lesson in LESSONS.items():
        live = set()
        for a in lesson["actions"]:
            refs = [a.get(k) for k in ("target", "around", "axes") if isinstance(a.get(k), str) and a.get(k) != "all"]
            refs += a["target"] if isinstance(a.get("target"), list) else []
            refs += [a["check"]["of"]] if a.get("check", {}).get("of") else []
            assert set(refs) <= live, (name, a)
            if a["do"] == "erase":
                live -= set(a["target"] if isinstance(a["target"], list) else [a["target"]])
            elif a.get("id"):
                assert a["id"] not in live, (name, "duplicate id", a["id"])
                live.add(a["id"])


def test_timeline_js_agrees_under_node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    script = ("const T = require(process.argv[1]); const fs = require('fs');"
              "const out = {}; for (const f of process.argv.slice(2)) out[f] = T.validate(JSON.parse(fs.readFileSync(f, 'utf8')));"
              "out.bad = T.validate({version: 1, title: 't', actions: [{do: 'highlight', target: 'ghost'},"
              " {do: 'write_math', latex: 'x', verified: true}]});"
              "console.log(JSON.stringify(out));")
    files = [str(ps.BOARD / "lessons" / f) for f in LESSONS]
    out = json.loads(subprocess.run([node, "-e", script, str(ps.BOARD / "lib/timeline.js"), *files],
                                    capture_output=True, text=True, encoding="utf-8", check=True).stdout)
    assert all(out[f] == [] for f in files)
    assert out["bad"] == ['action 1: target "ghost" isn\'t on the board at this point',
                          "action 2: a verified line must say what was checked (check)"]


# ---- every ✓ is honest: re-verify with SymPy ----------------------------------------------------------------
def _solutions(sp, expr, x):
    items = expr if isinstance(expr, (list, tuple)) else [expr]
    out = sp.EmptySet
    for eq in items:
        out = out.union(sp.solveset(eq.lhs - eq.rhs if isinstance(eq, sp.Equality) else eq, x, sp.S.Reals))
    return out


def verify_lesson(lesson):
    sp = pytest.importorskip("sympy")
    from sympy.parsing.sympy_parser import parse_expr
    by_id = {a["id"]: a for a in lesson["actions"] if a.get("id")}
    results = {}
    for a in lesson["actions"]:
        chk = a.get("check")
        if not chk:
            continue
        x = sp.Symbol(chk.get("variable", "x"))
        names = {"x": x, "Eq": sp.Eq, "sin": sp.sin, "cos": sp.cos}
        expr = parse_expr(chk["sympy"], local_dict=names, evaluate=False)
        rel = chk["relation"]
        if rel == "given":
            ok = True
        elif rel == "derivatives":
            ok = all(sp.simplify(sp.diff(u, x) - du) == 0 for u, du in expr)
        else:
            base = parse_expr(by_id[chk["of"]]["check"]["sympy"], local_dict=names, evaluate=False)
            if rel == "same_solutions":
                ok = _solutions(sp, expr, x) == _solutions(sp, base, x)
            elif rel == "equal":
                ok = sp.simplify(expr - base) == 0
            elif rel == "derivative_of":
                ok = sp.simplify(expr - sp.diff(base, x)) == 0
            else:
                ok = False
        results[a["id"]] = ok
    return results


@pytest.mark.parametrize("name", sorted(LESSONS))
def test_every_check_mark_is_verified_by_sympy(name):
    lesson = LESSONS[name]
    results = verify_lesson(lesson)
    verified = [a["id"] for a in lesson["actions"] if a.get("verified") is True]
    assert verified and set(verified) <= set(results), name
    assert {k: v for k, v in results.items() if k in verified and not v} == {}, name


def test_sympy_catches_a_wrong_step():
    lesson = copy.deepcopy(LESSONS["quadratic.json"])
    for a in lesson["actions"]:
        if a.get("id") == "eq1":
            a["check"]["sympy"] = "Eq((x - 1)*(x - 6), 0)"     # a plausible-looking wrong factorisation
    assert verify_lesson(lesson)["eq1"] is False
    lesson = copy.deepcopy(LESSONS["derivative.json"])
    for a in lesson["actions"]:
        if a.get("id") == "d1":
            a["check"]["sympy"] = "2*x*cos(x) + x**2*sin(x)"   # the classic mix-up
    assert verify_lesson(lesson)["d1"] is False


# ---- the page itself -----------------------------------------------------------------------------------------
def visible_verified(lesson):
    erased = set()
    for a in lesson["actions"]:
        if a["do"] == "erase":
            erased |= set(a["target"] if isinstance(a["target"], list) else [a["target"]])
    return [a["id"] for a in lesson["actions"] if a.get("verified") is True and a["id"] not in erased]


@pytest.mark.parametrize("name", sorted(LESSONS))
def test_the_board_plays_each_lesson_to_the_end(browser, name):
    lesson = LESSONS[name]
    with ps.static_server(ps.BOARD) as base:
        page = browser.new_page(viewport={"width": 1280, "height": 860})
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{base}/index.html?lesson={name[:-5]}&speed=20&mute=1&autoplay=1")
        assert ps.until(page, "['done','error'].includes(document.body.dataset.state) && document.body.dataset.state") == "done"
        state = page.evaluate("window.LanternBoard.state()")
        assert state["index"] == state["total"] == len(lesson["actions"]) and state["title"] == lesson["title"]
        on = page.evaluate("[...document.querySelectorAll('.check.on')].map((c) => c.dataset.slot)")
        assert sorted(on) == sorted(visible_verified(lesson))
        maths = [a for a in lesson["actions"] if a["do"] == "write_math"]
        assert page.evaluate("document.querySelectorAll('#items .katex').length") == len(
            [a for a in maths if a.get("id") in state["items"]])
        assert page.evaluate("document.querySelectorAll('#draw path, #draw ellipse').length") > 0
        assert "not a Meta model" in page.inner_text("#voice-note")
        assert page.inner_text("#caption") == next(a["say"] for a in reversed(lesson["actions"]) if a.get("say"))
        assert errors == []
        page.close()


def test_step_pause_restart_and_a_bad_lesson(browser):
    with ps.static_server(ps.BOARD) as base:
        page = browser.new_page()
        page.goto(f"{base}/index.html?lesson=quadratic&speed=20&mute=1")
        ps.until(page, "document.body.dataset.state === 'paused'")
        assert page.evaluate("window.LanternBoard.state().index") == 0
        page.click("#step")
        ps.until(page, "window.LanternBoard.state().index === 1 && document.body.dataset.state === 'paused'")
        assert page.inner_text("#items").startswith("Solve")
        page.keyboard.press("ArrowRight")
        ps.until(page, "window.LanternBoard.state().index === 2")
        page.click("#restart")
        ps.until(page, "document.body.dataset.state === 'done'")
        assert page.evaluate("window.LanternBoard.state().index") == len(LESSONS["quadratic.json"]["actions"])
        errs = page.evaluate("window.LanternBoard.load({version: 1, title: 'Broken', actions: [{do: 'highlight', target: 'ghost'}]})")
        assert errs and page.is_visible("#problems") and page.evaluate("document.body.dataset.state") == "error"
        page.close()
