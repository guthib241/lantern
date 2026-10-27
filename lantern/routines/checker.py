"""The static check every routine.py must pass before it may run (S4). A whitelist, not a blacklist.

Allowed, and nothing else:
  - one import: `from lantern.routines.runtime import browser, files, vault, check, step, uia` (any subset)
  - an optional module docstring, and one function `def run(params):`
  - inside it: `with step("s1", "what it does"):` blocks, calls to the runtime's allowlisted functions,
    `if` on params compared with constants (control flow is fixed when compiled: screen content can't
    add actions), `pass` and a bare `return`
  - expressions: constants, `params["name"]`, f-strings and `+` of those, calls to runtime functions
No other import, no `eval`/`exec`/`open`/`__import__`, no attribute access except the runtime's own
functions, no loops, assignments, lambdas, try/except, globals or dunders. So no network, no subprocess,
no file access except through the runtime (which goes through policy.py and the approval bus).
"""
from __future__ import annotations

import ast

RUNTIME = "lantern.routines.runtime"
API: dict[str, frozenset[str]] = {
    "browser": frozenset({"open", "fill", "upload", "submit"}),
    "files": frozenset({"move"}),
    "vault": frozenset({"get"}),
    "check": frozenset({"url_contains", "text_contains", "text_matches", "field_equals", "file_exists",
                        "file_missing"}),
    "uia": frozenset({"find", "click", "type"}),
}
NAMES = frozenset(API) | {"step"}
COMPARE_OPS = (ast.Eq, ast.NotEq, ast.In, ast.NotIn)


class CheckError(ValueError):
    pass


def check(code: str) -> list[str]:
    """Every problem found, as 'line N: why'. Empty means the routine may run."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"line {e.lineno}: syntax error: {e.msg}"]
    problems: list[str] = []
    body = list(tree.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    imports = [n for n in body if isinstance(n, (ast.Import, ast.ImportFrom))]
    funcs = [n for n in body if isinstance(n, ast.FunctionDef)]
    for n in body:
        if n not in imports and n not in funcs:
            problems.append(f"line {n.lineno}: only one import and `def run(params)` may be at the top level")
    for n in imports:
        if not (isinstance(n, ast.ImportFrom) and n.module == RUNTIME and n.level == 0
                and all(a.name in NAMES and a.asname is None for a in n.names)):
            problems.append(f"line {n.lineno}: the only import allowed is `from {RUNTIME} import ...` "
                            f"({', '.join(sorted(NAMES))})")
    if len(funcs) != 1 or funcs[0].name != "run":
        problems.append("there must be exactly one function, `run(params)`")
    else:
        f = funcs[0]
        a = f.args
        if (f.decorator_list or a.posonlyargs or a.kwonlyargs or a.vararg or a.kwarg or a.defaults
                or [x.arg for x in a.args] != ["params"] or f.returns):
            problems.append(f"line {f.lineno}: the function must be exactly `def run(params):`")
        for stmt in f.body:
            problems += _stmt(stmt)
    return problems


def _stmt(n: ast.stmt) -> list[str]:
    if isinstance(n, ast.Expr):
        if isinstance(n.value, ast.Constant) and isinstance(n.value.value, str):
            return []   # a docstring or a comment-like string
        if isinstance(n.value, ast.Call):
            return _call(n.value)
        return [f"line {n.lineno}: only calls to the runtime may stand alone"]
    if isinstance(n, ast.With):
        out = []
        if len(n.items) != 1 or n.items[0].optional_vars is not None:
            out.append(f"line {n.lineno}: `with` takes exactly one `step(...)` and no `as`")
        else:
            ctx = n.items[0].context_expr
            if not (isinstance(ctx, ast.Call) and isinstance(ctx.func, ast.Name) and ctx.func.id == "step"
                    and not ctx.keywords and 1 <= len(ctx.args) <= 2
                    and all(isinstance(x, ast.Constant) and isinstance(x.value, str) for x in ctx.args)):
                out.append(f"line {n.lineno}: `with` is only for `step(\"id\", \"what it does\")`")
        for s in n.body:
            out += _stmt(s)
        return out
    if isinstance(n, ast.If):
        out = _condition(n.test)
        for s in n.body + n.orelse:
            out += _stmt(s)
        return out
    if isinstance(n, ast.Pass):
        return []
    if isinstance(n, ast.Return) and n.value is None:
        return []
    return [f"line {n.lineno}: `{type(n).__name__}` isn't allowed in a routine (no loops, assignments, "
            "try/except, imports or definitions inside run)"]


def _condition(n: ast.expr) -> list[str]:
    """Branches may only depend on params (fixed when the routine starts), never on what's on screen."""
    if isinstance(n, ast.BoolOp):
        return [p for v in n.values for p in _condition(v)]
    if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not):
        return _condition(n.operand)
    if isinstance(n, ast.Compare) and all(isinstance(op, COMPARE_OPS) for op in n.ops):
        return [p for v in [n.left, *n.comparators] for p in _value(v, allow_calls=False)]
    if _is_param(n):
        return []
    return [f"line {n.lineno}: an `if` may only compare params with constants"]


def _call(n: ast.Call) -> list[str]:
    f = n.func
    out = []
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in API:
        if f.attr not in API[f.value.id]:
            out.append(f"line {n.lineno}: `{f.value.id}.{f.attr}` isn't part of the routine runtime")
    else:
        out.append(f"line {n.lineno}: only the runtime's functions may be called "
                   f"({', '.join(f'{k}.*' for k in sorted(API))})")
    for a in n.args:
        if isinstance(a, ast.Starred):
            out.append(f"line {n.lineno}: no *args")
        else:
            out += _value(a)
    for k in n.keywords:
        if k.arg is None:
            out.append(f"line {n.lineno}: no **kwargs")
        else:
            out += _value(k.value)
    return out


def _is_param(n: ast.expr) -> bool:
    return (isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name) and n.value.id == "params"
            and isinstance(n.slice, ast.Constant) and isinstance(n.slice.value, str))


def _value(n: ast.expr, allow_calls: bool = True) -> list[str]:
    if isinstance(n, ast.Constant) and isinstance(n.value, (str, int, float, bool, type(None))):
        return []
    if _is_param(n):
        return []
    if isinstance(n, ast.JoinedStr):
        return [p for v in n.values for p in _value(v, allow_calls)]
    if isinstance(n, ast.FormattedValue) and n.format_spec is None and n.conversion == -1:
        return _value(n.value, allow_calls)
    if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add):
        return _value(n.left, allow_calls) + _value(n.right, allow_calls)
    if isinstance(n, (ast.List, ast.Tuple)):
        return [p for v in n.elts for p in _value(v, allow_calls)]
    if allow_calls and isinstance(n, ast.Call):
        return _call(n)
    return [f"line {getattr(n, 'lineno', '?')}: `{type(n).__name__}` isn't allowed here "
            "(only constants, params[\"name\"], f-strings, + and runtime calls)"]
