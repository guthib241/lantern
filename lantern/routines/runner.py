"""Running a routine (S4): render code from a contract, load checked code, approve the plan once, run it.

  render(contract)  the routine.py Lantern writes itself from a contract (no model). It is also the yardstick
                    for code a model writes: `same_calls` must find exactly the same runtime calls, in the
                    same steps and order, or the model's code is not used.
  load(code, names) checks the code (checker.py), then runs it with no builtins at all and an import hook
                    that hands out only the runtime names for this run.
  plan_for(...)     the one approval for a live run: every value it will type (as hashes in the payload,
                    in plain words in the summary) and every red action, by step.
  execute(...)      runs it; a step that fails ends the run with a StepFailed for the caller (compiler.py)
                    to heal or report. A dry run ends cleanly at its first red action.
"""
from __future__ import annotations

import ast
import re
import time
from dataclasses import dataclass, field
from pathlib import PurePath
from types import SimpleNamespace
from typing import Any

from .checker import RUNTIME, CheckError, check
from .contract import ACTIONS, PLACEHOLDER, fill_in, placeholders
from .runtime import DryStop, RoutineContext, StepFailed, sha, sort_moves

CHECK_CALLS = {"browser.url_contains": "check.url_contains", "browser.text_contains": "check.text_contains",
               "browser.text_matches": "check.text_matches", "browser.field_equals": "check.field_equals",
               "files.exists": "check.file_exists", "files.missing": "check.file_missing"}
ARG_ORDER = {"browser.open": ("url",), "browser.fill": ("field", "value"), "browser.upload": ("field", "path"),
             "browser.submit": ("button",), "files.move": ("source", "destination"),
             "files.sort": ("folder", "match", "to")}


# ---- code from a contract ---------------------------------------------------------------
def expr(value: Any) -> str:
    """A contract value as a Python expression: literals, params["x"], vault.get("key"), joined with +."""
    if not isinstance(value, str):
        return repr(value)
    parts, pos = [], 0
    for m in PLACEHOLDER.finditer(value):
        if m.start() > pos:
            parts.append(repr(value[pos:m.start()]))
        key = m.group(1)
        parts.append(f'vault.get({key[len("vault."):]!r})' if key.startswith("vault.") else f"params[{key!r}]")
        pos = m.end()
    if pos < len(value) or not parts:
        parts.append(repr(value[pos:]))
    return " + ".join(parts)


def _call(action: str, args: dict) -> str:
    order = ARG_ORDER[action]
    pos = ", ".join(expr(args[k]) for k in order)
    hints = "".join(f", {k}={expr(v)}" for k, v in args.items() if k not in order and k in ACTIONS[action][1])
    return f"{action}({pos}{hints})"


def _check(c: dict) -> str:
    return f"{CHECK_CALLS[c['check']]}({', '.join(expr(v) for v in c['args'].values())})"


def render(contract: dict) -> str:
    src = contract.get("source") or {}
    lines = [f'"""{_doc(contract.get("title") or contract["name"])} (compiled by Lantern'
             + (f' from task #{src["task_id"]}' if src.get("task_id") else "") + ')."""',
             f"from {RUNTIME} import browser, check, files, step, vault", "", "", "def run(params):"]
    for s in contract["steps"]:
        lines.append(f"    with step({s['id']!r}, {_doc(s.get('say', ''))!r}):")
        lines.append(f"        {_call(s['action'], s['args'])}")
        lines += [f"        {_check(c)}" for c in s.get("post") or []]
    if contract.get("success"):
        lines.append('    with step("done", "Check that it worked"):')
        lines += [f"        {_check(c)}" for c in contract["success"]]
    return "\n".join(lines) + "\n"


def _doc(text: str) -> str:
    return re.sub(r'["\\\n\r]', " ", str(text))[:120].strip()


def calls_of(code: str) -> list[tuple[str, str, tuple[str, ...]]]:
    """(step id, runtime function, arguments as source) for every call, in order."""
    out: list[tuple[str, str, tuple[str, ...]]] = []
    tree = ast.parse(code)

    def walk(stmts, sid):
        for n in stmts:
            if isinstance(n, ast.With):
                ctx = n.items[0].context_expr
                walk(n.body, ctx.args[0].value if isinstance(ctx, ast.Call) and ctx.args else sid)
            elif isinstance(n, ast.If):
                walk(n.body, sid)
                walk(n.orelse, sid)
            elif isinstance(n, ast.Expr) and isinstance(n.value, ast.Call):
                f = n.value.func
                name = ast.unparse(f)
                args = tuple(ast.unparse(a) for a in n.value.args) + tuple(
                    f"{k.arg}={ast.unparse(k.value)}" for k in sorted(n.value.keywords, key=lambda k: k.arg or ""))
                out.append((sid, name, args))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            walk(node.body, None)
    return out


def same_calls(code: str, contract: dict) -> list[str]:
    """Model-written code must make exactly the calls Lantern's own rendering makes."""
    try:
        mine, want = calls_of(code), calls_of(render(contract))
    except SyntaxError as e:
        return [f"syntax error: {e.msg}"]
    if mine == want:
        return []
    i = next((i for i, (a, b) in enumerate(zip(mine, want)) if a != b), min(len(mine), len(want)))
    got = mine[i] if i < len(mine) else "nothing"
    exp = want[i] if i < len(want) else "nothing"
    return [f"call {i + 1} differs from the contract: got {got}, expected {exp}"]


# ---- loading ----------------------------------------------------------------------------
def load(code: str, names: dict):
    """Check, then run the module with no builtins; the only import that works is the runtime's."""
    problems = check(code)
    if problems:
        raise CheckError("; ".join(problems[:5]))
    runtime = SimpleNamespace(**names)

    def only_runtime(name, globals=None, locals=None, fromlist=(), level=0):
        if name == RUNTIME and level == 0:
            return runtime
        raise ImportError(f"routines may only import {RUNTIME}")
    env: dict = {"__builtins__": {"__import__": only_runtime}}
    exec(compile(code, "routine.py", "exec"), env)   # noqa: S102 -- checked code, no builtins
    return env["run"]


# ---- the plan the user approves ------------------------------------------------------------
def resolve_params(contract: dict, given: dict | None = None) -> tuple[dict, list[str]]:
    """Defaults plus what the goal or the user gave; secrets without a value are reported missing."""
    given = dict(given or {})
    params, missing = {}, []
    for p in contract.get("params") or []:
        v = given.get(p["name"], p.get("default"))
        if p.get("type") == "enum" and v not in (p.get("values") or []):
            v = p.get("default")
        if v in (None, ""):
            missing.append(p["name"])
        params[p["name"]] = v
    return params, missing


def plan_for(routine, params: dict, vault: dict, target: str = "", policy=None) -> dict:
    c = routine.contract
    fields, red, values, shown = [], [], {}, []
    moves: list[list[str]] = []      # files.sort: the exact moves the user approves, found now (S5)
    taken: set[str] = set()
    for s in c["steps"]:
        a = s["args"]
        if s["action"] == "files.sort":
            if policy is None:
                raise ValueError("a files routine needs policy.py to plan its moves")
            found = sort_moves(policy, fill_in(a["folder"], params, vault), a["match"], a["to"], taken)
            taken |= {src for src, _ in found}
            moves += [[src, dst] for src, dst in found]
            if found:
                red.append(s["id"])
            continue
        if s["action"] in ("browser.fill", "browser.upload"):
            v = fill_in(a.get("value", a.get("path")), params, vault)
            values[s["id"]] = sha(v)
            fields.append([s["id"], a["field"], sha(v)])
            specs = [next((p for p in c.get("params") or [] if p["name"] == k), {}) for k in placeholders(a.get("value", ""))]
            secret = any(p.get("type") == "secret" for p in specs)
            label = next((p.get("labels", {}).get(v) for p in specs if p.get("labels", {}).get(v)), None)
            shown.append(f"{a['field']}: {'(secret)' if secret else _short(label or v)}")
        elif ACTIONS[s["action"]][2] == "red":
            red.append(s["id"])
    payload = {"routine": routine.name, "version": routine.version, "target": target,
               "params": {k: v for k, v in params.items()
                          if next((p for p in c.get("params") or [] if p["name"] == k), {}).get("type") != "secret"},
               "fields": fields, "red": [[sid, next(s["action"] for s in c["steps"] if s["id"] == sid)] for sid in red]}
    if c.get("kind") == "files":
        payload["moves"] = moves
        groups: dict[str, int] = {}
        for _, dst in moves:
            folder = PurePath(dst).parent.name
            groups[folder] = groups.get(folder, 0) + 1
        summary = (f"Run the learned routine '{c.get('title') or routine.name}' v{routine.version} on {target} "
                   f"(no model calls): move {len(moves)} file{'s' if len(moves) != 1 else ''}"
                   + (": " + "; ".join(f"{n} → {g}" for g, n in sorted(groups.items(), key=lambda x: -x[1])[:6])
                      if moves else "") + ".")
        return {"payload": payload, "summary": summary, "red": red, "values": values, "moves": moves}
    actions = sum(1 for s in c["steps"] if s["id"] in red)
    summary = (f"Run the learned routine '{c.get('title') or routine.name}' v{routine.version} on {target or 'this computer'} "
               f"(no model calls): {actions} submit{'s' if actions != 1 else ''}"
               + (f", with {'; '.join(shown[:12])}{' …' if len(shown) > 12 else ''}" if shown else "") + ".")
    return {"payload": payload, "summary": summary, "red": red, "values": values}


def _short(v: str, n: int = 40) -> str:
    v = str(v)
    return v if len(v) <= n else v[: n - 1] + "…"


# ---- running ---------------------------------------------------------------------------------
@dataclass
class RunResult:
    ok: bool
    mode: str
    log: list = field(default_factory=list)
    patches: dict = field(default_factory=dict)
    failed: StepFailed | None = None
    acted: bool = False
    declined: bool = False
    stopped_at: str | None = None   # a dry run's first red step
    seconds: float = 0.0
    moved: list = field(default_factory=list)   # files.sort: what moved
    left: list = field(default_factory=list)    # files.sort: what arrived after the approval and stayed

    def public(self) -> dict:
        return {"ok": self.ok, "mode": self.mode, "steps": len(self.log), "acted": self.acted,
                "declined": self.declined, "stopped_at": self.stopped_at, "seconds": round(self.seconds, 2),
                "healed": self.patches, "failed": None if self.failed is None else {
                    "step": self.failed.step_id, "kind": self.failed.kind, "why": self.failed.why}}


def execute(routine, params: dict, ctx: RoutineContext) -> RunResult:
    t0 = time.monotonic()
    res = RunResult(ok=False, mode=ctx.mode)
    try:
        if not ctx.ask_approval():
            res.declined = True
            return res
        fn = load(routine.code, ctx.names())
        fn(dict(params))
        res.ok = True
    except DryStop as e:
        res.ok, res.stopped_at = True, e.step_id
    except StepFailed as e:
        res.failed = e
    except CheckError as e:
        res.failed = StepFailed(None, f"the routine's code failed the static check: {e}", "refused")
    finally:
        ctx.close()
        res.log, res.patches, res.acted = ctx.log, ctx.patches, ctx.acted
        res.moved, res.left = ctx.moved, ctx.left
        res.seconds = time.monotonic() - t0
    return res
