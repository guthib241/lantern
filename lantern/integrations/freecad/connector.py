"""The Lantern side of the FreeCAD connector: config, risk tiers, approvals, and the verify loop.

How it plugs in (see docs/FREECAD.md, "Merge hooks"):
  1. ~/.lantern/mcp.json gets the "freecad" server from server_spec() (or
     config/mcp/freecad.example.json). Lantern's existing MCP client starts it and offers its tools
     to Muse Spark as deferred tools behind tool_search; the green tools are listed as read_only, so
     they run without asking. Without any other change this already works: yellow and red tools go
     through the generic MCP approval, and the server itself refuses paths outside writable zones.
  2. With the one-line hook in Toolbox.mcp_call (ref.server == "freecad" -> FreeCADConnector.call),
     calls get proper tiers (one-tap yellow, red with the exact file list), and every change and the
     final check go through the verify loop: numbers from FreeCAD, then Muse Spark looking at the
     render with the user's goal. One fix attempt after a failed check, then an honest report.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ... import models
from . import tiers, verify

REPO = Path(__file__).resolve().parents[3]
SERVER_NAME = "freecad"


def server_spec(cfg, *, backend: str = "auto", freecad_cmd: str = "") -> dict:
    """The mcp.json entry for Lantern's FreeCAD server, built from Lantern's own config."""
    env = {"LANTERN_FREECAD_BACKEND": backend,
           "LANTERN_FREECAD_ZONES": str(cfg.zones_file),
           "LANTERN_FREECAD_ROOTS": os.pathsep.join(str(r) for r in cfg.roots),
           "LANTERN_FREECAD_RENDERS": str(Path(cfg.media_dir) / "freecad")}
    if freecad_cmd:
        env["LANTERN_FREECAD_CMD"] = freecad_cmd
    return {"command": sys.executable, "args": ["-m", "lantern.integrations.freecad.server"], "cwd": str(REPO),
            "env": env, "read_only": list(tiers.GREEN_TOOLS)}


def add_to_mcp_json(cfg, path: str | Path | None = None, **kw) -> Path:
    """Write (or update) the freecad entry in Lantern's mcp.json; other servers are kept."""
    p = Path(path or cfg.mcp_config).expanduser()
    data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    data.setdefault("servers", {})[SERVER_NAME] = server_spec(cfg, **kw)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")   # Lantern's own config file
    return p


@dataclass
class TaskState:
    task_id: int | None = None
    goal: str = ""
    expect: dict | None = None
    checks_failed: int = 0
    last_verdict: dict | None = None
    renders: list[str] = field(default_factory=list)


class FreeCADConnector:
    """Wraps calls to the freecad MCP server with tiers, approvals and verification."""

    def __init__(self, policy, client=None, memory=None, *, visual: str = "every_change", max_fixes: int = 1,
                 on_event=None):
        if visual not in ("every_change", "on_check", "off"):
            raise ValueError("visual must be every_change, on_check or off")
        self.policy, self.client, self.memory = policy, client, memory
        self.visual, self.max_fixes = visual, max_fixes
        self.on_event = on_event or (lambda kind, data: None)
        self.tasks: dict[Any, TaskState] = {}

    # ---- the hook target ------------------------------------------------------------------
    def call(self, toolbox, function: str, args: dict) -> dict:
        """Run a freecad.* tool for the agent. Always returns a result dict: a refusal or an unexpected
        error comes back to the model as {"error": ...} so it can recover, never as a crashed task
        (live run, Oct 27: format "STL" raised KeyError and failed the whole task)."""
        try:
            return self._call(toolbox, function, args)
        except Exception as e:
            from ...mcp_client import MCPError
            kind = "FreeCAD" if isinstance(e, MCPError) else type(e).__name__
            return {"error": f"{kind}: {str(e)[:300]}", "tier": "error"}

    def _call(self, toolbox, function: str, args: dict) -> dict:
        ref = toolbox.mcp.tools[function]
        tool = ref.tool
        state = self._state(toolbox)
        try:
            rv = tiers.review(tool, args, self.policy, exists=self.policy.exists,
                              checks=state.last_verdict if tool == "export" else None)
        except tiers.TierError as e:
            return {"error": str(e), "tier": "refused"}
        if rv.tier != tiers.GREEN:
            commit = toolbox._approve(f"freecad_{tool}", rv.summary, rv.payload)
            if commit is None:
                return {"done": False, "reason": "user declined", "tier": rv.tier}
            if refused := toolbox._commit(commit, rv.payload):
                return {**refused, "tier": rv.tier}
            if rv.overwrites:
                args = {**args, "overwrite": True}   # the user approved replacing exactly these files
        out = toolbox.mcp.call(function, args)
        result = self._parse(out)
        result["tier"] = rv.tier
        if out.get("is_error"):
            result.setdefault("error", out.get("text", "FreeCAD reported an error")[:500])
            return result
        if tool == "check_model":
            return self._final_check(state, result)
        if rv.tier == tiers.YELLOW and tool not in ("new_document", "save") and self.visual == "every_change":
            result["verify"] = self._verify(state, result, final=False)
        if tool == "export":
            result["checks_before_export"] = state.last_verdict or {"passed": None, "note": "never checked"}
        return result

    # ---- verify loop -----------------------------------------------------------------------
    def _state(self, toolbox) -> TaskState:
        key = getattr(toolbox, "task_id", None)
        st = self.tasks.setdefault(key, TaskState(task_id=key))
        if not st.goal and self.memory is not None and key:
            try:
                st.goal = self.memory.get_task(key).goal
            except KeyError:
                pass
        return st

    def expectations_for(self, state: TaskState) -> dict:
        if state.expect is None:
            if self.client is None or not state.goal:
                state.expect = {}
            else:
                state.expect, r = verify.expectations(self.client, state.goal)
                self._usage(r, state.task_id)
        return state.expect

    def _verify(self, state: TaskState, result: dict, *, final: bool) -> dict:
        measure = result.get("measure") or {}
        if not measure:
            return {"passed": None, "note": "nothing to measure"}
        expect = result.get("expect") or self.expectations_for(state)
        numeric = verify.numeric_checks(expect, measure)
        visual = None
        png_path = result.get("render")
        if png_path and self.client is not None and (final or self.visual == "every_change"):
            try:
                png = Path(png_path).read_bytes()   # Lantern's own render in its media folder
                visual, r = verify.visual_check(self.client, state.goal or "(no goal given)", png, measure, numeric)
                self._usage(r, state.task_id)
            except Exception as e:   # the numbers still decide; say the eyes were missing
                visual = None
                result.setdefault("notes", []).append(f"visual check failed to run: {type(e).__name__}")
        verdict = verify.decide(numeric, visual).as_dict()
        verdict["stage"] = "final" if final else "after change"
        if png_path:
            state.renders.append(png_path)
        self.on_event("cad_verify", {"stage": verdict["stage"], "passed": verdict["passed"], "failed": verdict["failed"],
                                     "render": png_path, "badge": models.badge(models.SPARK)})
        return verdict

    def _final_check(self, state: TaskState, result: dict) -> dict:
        verdict = self._verify(state, result, final=True)
        state.last_verdict = verdict
        if verdict["passed"]:
            verdict["say"] = "All checks passed. You may tell the user it's done (mention the numbers)."
        else:
            state.checks_failed += 1
            left = self.max_fixes - (state.checks_failed - 1)
            if left > 0:
                verdict["say"] = (f"Checks failed: {'; '.join(verdict['failed'][:4])}. You have {left} fix attempt: "
                                  "change the model, then call check_model again.")
            else:
                verdict["say"] = ("Checks failed again. Stop changing the model and tell the user honestly what is "
                                  "still wrong. Do not say it is done.")
                verdict["give_up"] = True
        return {"check": verdict, "render": result.get("render"), "measure": result.get("measure")}

    # ---- helpers -----------------------------------------------------------------------------
    @staticmethod
    def _parse(out: dict) -> dict:
        if isinstance(out.get("structured"), dict):
            s = out["structured"]
            return dict(s.get("result", s)) if isinstance(s.get("result", s), dict) else {"result": s}
        try:
            v = json.loads(out.get("text") or "{}")
            return v if isinstance(v, dict) else {"result": v}
        except json.JSONDecodeError:
            return {"text": (out.get("text") or "")[:2000]}

    def _usage(self, response, task_id: int | None) -> None:
        """Verify calls cost money like any other: logged to the task, so receipts and budgets see them."""
        u = models.Usage.from_response(getattr(response, "usage", None))
        cost = models.token_cost(models.SPARK, u)
        if self.memory is not None and response is not None:
            self.memory.log_usage(task_id, models.SPARK, u.input_tokens, u.cached_tokens, u.output_tokens, cost)
        self.on_event("usage", {"model": models.SPARK, "badge": models.badge(models.SPARK), "cost_usd": cost,
                                "input_tokens": u.input_tokens, "output_tokens": u.output_tokens, "for": "cad verify"})
