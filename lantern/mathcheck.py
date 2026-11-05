"""Verified math (S16): every equation step is checked with SymPy before the board may show it with a ✓.

A step's `check` (the board's lesson schema) says what it is in SymPy syntax and how it relates to an earlier line:
  given            the problem as stated (nothing to check)
  equal            the same expression as line `of`            simplify(new - old) == 0
  same_solutions   the same solutions as equation line `of`    solveset over the reals, compared
  derivative_of    the derivative of line `of`                 simplify(new - d/dx old) == 0
  derivatives      a list of [u, u'] pairs, each one right      simplify(d/dx u - u') == 0
The expressions come from a model, and SymPy's parser evaluates what it reads, so nothing reaches it unless it
passes a whitelist first: digits, the operators + - * / ^ ( ) [ ] , . = and spaces, and only the names below.
No quotes, no underscores, no attribute access. Each check also has a time limit.
"""
from __future__ import annotations

import re
import threading
from typing import Any

NAMES = ("x", "y", "z", "t", "n", "a", "b", "c", "k", "Eq", "sin", "cos", "tan", "exp", "log", "ln", "sqrt", "pi",
         "E", "Abs", "asin", "acos", "atan", "sinh", "cosh", "tanh")
SAFE = re.compile(r"[0-9A-Za-z+\-*/^()\[\],.= ]{1,300}")
RELATIONS = ("given", "equal", "same_solutions", "derivative_of", "derivatives")
TIME_LIMIT = 5.0


class MathError(ValueError):
    """The step can't be checked; the message says why."""


def safe(text: str) -> str:
    """The expression if it may be parsed, else MathError."""
    text = str(text or "").strip()
    if not SAFE.fullmatch(text):
        raise MathError("only numbers, + - * / ^ ( ) [ ] , . = and a few names are allowed")
    for name in re.findall(r"[A-Za-z]+", text):
        if name not in NAMES:
            raise MathError(f"'{name}' isn't a name the checker knows")
    if re.search(r"(?<![0-9])\.|\.(?![0-9])", text):
        raise MathError("a dot may only be a decimal point")
    return text


def parse(text: str, variable: str = "x"):
    import sympy as sp
    from sympy.parsing.sympy_parser import (convert_xor, implicit_multiplication_application, parse_expr,
                                            standard_transformations)
    names: dict[str, Any] = {n: sp.Symbol(n) for n in ("x", "y", "z", "t", "n", "a", "b", "c", "k")}
    names.update(Eq=sp.Eq, sin=sp.sin, cos=sp.cos, tan=sp.tan, exp=sp.exp, log=sp.log, ln=sp.log, sqrt=sp.sqrt,
                 pi=sp.pi, E=sp.E, Abs=sp.Abs, asin=sp.asin, acos=sp.acos, atan=sp.atan, sinh=sp.sinh, cosh=sp.cosh,
                 tanh=sp.tanh)
    tf = standard_transformations + (convert_xor, implicit_multiplication_application)
    try:
        return parse_expr(safe(text), local_dict=names, global_dict={"__builtins__": {}, **_sympy_globals()},
                          transformations=tf, evaluate=False)
    except MathError:
        raise
    except Exception as e:   # SymPy's own parse errors
        raise MathError(f"SymPy couldn't read it: {type(e).__name__}") from e


def _sympy_globals() -> dict:
    """What SymPy's own parser writes into the code it evaluates (number and symbol wrappers, and the
    Add/Mul/Pow it uses for evaluate=False). Nothing else is reachable."""
    import sympy as sp
    return {"Integer": sp.Integer, "Float": sp.Float, "Rational": sp.Rational, "Symbol": sp.Symbol,
            "Add": sp.Add, "Mul": sp.Mul, "Pow": sp.Pow}


def _solutions(expr, x):
    import sympy as sp
    items = list(expr) if isinstance(expr, (list, tuple)) else [expr]
    out = sp.EmptySet
    for eq in items:
        e = eq.lhs - eq.rhs if isinstance(eq, sp.Equality) else eq
        out = out.union(sp.solveset(e, x, sp.S.Reals))
    return out


def _within(fn, seconds: float = TIME_LIMIT):
    box: dict = {}

    def run():
        try:
            box["value"] = fn()
        except Exception as e:   # reported as a failed check, with the reason
            box["error"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        raise MathError("SymPy took too long to check this step")
    if "error" in box:
        e = box["error"]
        raise e if isinstance(e, MathError) else MathError(f"SymPy couldn't check it: {type(e).__name__}")
    return box["value"]


def check_step(check: dict, earlier: dict[str, dict]) -> tuple[bool, str]:
    """(ok, why) for one step; `earlier` maps ids of earlier lines to their checks."""
    import sympy as sp
    rel = check.get("relation")
    if rel not in RELATIONS:
        return False, f"unknown relation {rel!r}"
    var = str(check.get("variable") or "x")
    if var not in ("x", "y", "z", "t", "n", "a", "b", "c", "k"):
        return False, f"unknown variable {var!r}"
    x = sp.Symbol(var)
    try:
        new = parse(check.get("sympy", ""), var)
        if rel == "given":
            return True, "the problem as given"
        if rel == "derivatives":
            pairs = list(new) if isinstance(new, (list, tuple)) else []
            if not pairs or not all(isinstance(p, (list, tuple)) and len(p) == 2 for p in pairs):
                return False, "derivatives needs a list of [u, u'] pairs"
            ok = _within(lambda: all(sp.simplify(sp.diff(u, x) - du) == 0 for u, du in pairs))
            return (True, "each derivative checks out") if ok else (False, "a derivative in the list is wrong")
        base_check = earlier.get(str(check.get("of", "")))
        if base_check is None:
            return False, f"line {check.get('of')!r} isn't an earlier checked line"
        old = parse(base_check.get("sympy", ""), var)
        if rel == "equal":
            ok = _within(lambda: sp.simplify(new - old) == 0)
            return (True, "equal to the line before") if ok else (False, "it isn't equal to the line it says it is")
        if rel == "same_solutions":
            ok = _within(lambda: _solutions(new, x) == _solutions(old, x))
            return (True, "same solutions") if ok else (False, "its solutions differ from the line it comes from")
        ok = _within(lambda: sp.simplify(new - sp.diff(old, x)) == 0)
        return (True, "the derivative checks out") if ok else (False, "it isn't the derivative of that line")
    except MathError as e:
        return False, str(e)


def verify(lesson: dict) -> dict[str, tuple[bool, str]]:
    """Every write_math step with a check, in order: id -> (ok, why). A step may only build on checked lines."""
    earlier: dict[str, dict] = {}
    out: dict[str, tuple[bool, str]] = {}
    for a in lesson.get("actions") or []:
        chk = a.get("check")
        if a.get("do") != "write_math" or not isinstance(chk, dict):
            continue
        ok, why = check_step(chk, earlier)
        out[str(a.get("id"))] = (ok, why)
        if ok:
            earlier[str(a.get("id"))] = chk
    return out
