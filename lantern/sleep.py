"""Sleep (S11): while you sleep, Lantern checks every learned routine, repairs what drifted, and tells you in the morning.

  1. Shadow replay. Each trusted routine runs on a sandbox copy of its site when one is set up
     (LANTERN_ROUTINE_SANDBOX: every step, nothing real happens), otherwise dry on the real site, stopping before
     its first submit or move: nothing is sent, submitted or moved. A field or button that moved gets the S4 repair
     (one cheap Muse Spark call picks the new target from the page), and the healed version is verified before it
     replaces the trusted one, so the morning's run works first time.
  2. Suggestions, with no model call: goals done twice or more that were never learned ("learn 14"), and routines
     that do the same steps (candidates to merge into one routine with a parameter).
  3. The morning digest, in Lantern's voice: what was checked, what was fixed, what needs you. It goes to the
     desktop and the phone. Measured numbers only.
Runs from `lantern --sleep`, `POST /sleep`, or by itself each night at LANTERN_SLEEP_AT ("03:30" by default with
`lantern --serve`; "off" turns it off; other ways of running Lantern never sleep on their own).
"""
from __future__ import annotations

import os
import time
from typing import Any

from .routines.contract import goal_words, plan_of
from .routines.runner import execute, resolve_params
from .routines.runtime import RoutineContext

GOAL = "Sleep: check and repair learned routines"
DEFAULT_AT = "03:30"


def sleep_at(env: dict | None = None) -> tuple[int, int] | None:
    """The nightly time (local), or None when it's off."""
    v = (env if env is not None else os.environ).get("LANTERN_SLEEP_AT", "off").strip().lower()
    if v in ("", "off", "no", "0"):
        return None
    try:
        h, m = (int(x) for x in v.split(":"))
    except ValueError:
        return None
    return (h, m) if 0 <= h < 24 and 0 <= m < 60 else None


def due(memory, now: float | None = None, env: dict | None = None) -> bool:
    """Past tonight's time, and no sleep has run since then."""
    at = sleep_at(env)
    if at is None:
        return False
    now = time.time() if now is None else now
    lt = time.localtime(now)
    start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, at[0], at[1], 0, 0, 0, -1))
    if now < start:
        return False
    return not any(t.goal == GOAL and t.created_at >= start for t in memory.recent_tasks(50))


def shadow(compiler, routine, *, client=None, ladder=None, task_id: int | None = None) -> dict:
    """One routine, replayed where nothing real can happen. Returns {"name", "ok", "mode", "healed", "why"}."""
    c = routine.contract
    out: dict[str, Any] = {"name": routine.name, "title": routine.meta.get("title") or routine.name,
                           "version": routine.version}
    params, missing = resolve_params(c)
    if missing:
        return {**out, "ok": None, "why": f"needs {', '.join(missing)} each time, so it can't be replayed alone"}
    sandbox = compiler.sandbox(params.get("base_url", "")) if c.get("kind") == "web" and compiler.sandbox else None
    if sandbox:
        params["base_url"] = sandbox.rstrip("/")

    def heal(step_id, kind, old, candidates):
        step = next((s for s in c["steps"] if s["id"] == step_id), {"say": ""})
        return compiler.repair(step, kind, old, candidates, client=client, ladder=ladder, task_id=task_id)
    ctx = RoutineContext(mode="sandbox" if sandbox else "dry", policy=compiler.core.policy, vault=compiler.vault(),
                         driver_factory=compiler._driver_factory(), url_ok=compiler._url_ok, heal=heal)
    with compiler._browser():
        res = execute(routine, params, ctx)
    out["mode"] = ctx.mode
    if not res.ok:
        f = res.failed
        return {**out, "ok": False, "why": f"step {getattr(f, 'step_id', '?')}: {getattr(f, 'why', 'it stopped')}"}
    if res.patches:
        healed = compiler.heal(routine, res.patches, task_id=task_id)
        if healed is None or not healed.trusted:
            return {**out, "ok": False, "why": "the page changed and the repair didn't check out"}
        what = "; ".join(f"'{p.get('field') or p.get('button')}'" for p in res.patches.values())
        return {**out, "ok": True, "healed": {"steps": sorted(res.patches), "now": what, "version": healed.version}}
    return {**out, "ok": True}


def suggestions(compiler, memory) -> dict:
    """Repeated goals never learned; routines that do the same steps."""
    groups: dict[str, list] = {}
    for t in memory.recent_tasks(200):
        if t.status == "done" and not t.goal.startswith(("Learn task #", "Re-learn ", GOAL, "Scan this photo")):
            groups.setdefault(goal_words(t.goal).lower(), []).append(t)
    learn = []
    for goal, ts in groups.items():
        if len(ts) < 2 or compiler.match(goal) is not None:
            continue
        latest = max(ts, key=lambda t: t.created_at)
        if compiler.learnable(latest.id):
            learn.append({"goal": goal, "times": len(ts), "task_id": latest.id})
    merge, seen = [], {}
    for r in compiler.store.all():
        if r.trusted:
            key = repr(plan_of(r.contract))
            if key in seen:
                merge.append([seen[key], r.name])
            else:
                seen[key] = r.name
    return {"learn": learn, "merge": merge}


def digest(report: dict) -> str:
    checked, healed = report["checked"], [x for x in report["checked"] if x.get("healed")]
    broken = [x for x in checked if x.get("ok") is False]
    if not checked and not report["learn"] and not report["merge"]:
        return "🌙 Nothing to check last night: no learned routines yet."
    parts = [f"🌙 While you slept I checked {len(checked)} routine{'s' if len(checked) != 1 else ''}"
             + (" on a copy of each site" if checked and all(x.get("mode") == "sandbox" for x in checked if "mode" in x)
                else "") + "."]
    for x in healed:
        parts.append(f"{x['title']}: the page had changed, so I re-pointed {len(x['healed']['steps'])} "
                     f"step{'s' if len(x['healed']['steps']) != 1 else ''} to {x['healed']['now']} and re-checked it "
                     f"(now v{x['healed']['version']}).")
    for x in broken:
        parts.append(f"{x['title']} needs you: {x['why']}.")
    if checked and not healed and not broken:
        parts.append("All of them still work.")
    for s in report["learn"][:3]:
        parts.append(f"You've done “{s['goal']}” {s['times']} times; say “learn {s['task_id']}” and it runs with no "
                     "model calls.")
    for a, b in report["merge"][:2]:
        parts.append(f"{a} and {b} do the same steps; they could be one routine.")
    return " ".join(parts)


def run(compiler, memory, *, client=None, ladder=None, task_id: int | None = None) -> dict:
    report: dict[str, Any] = {"checked": [], "started_at": time.time()}
    for r in compiler.store.all():
        if not r.trusted:
            continue
        try:
            report["checked"].append(shadow(compiler, r, client=client, ladder=ladder, task_id=task_id))
        except Exception as e:   # one routine's trouble never stops the night's check
            report["checked"].append({"name": r.name, "title": r.meta.get("title") or r.name, "ok": False,
                                      "why": f"{type(e).__name__}: {str(e)[:120]}"})
    report.update(suggestions(compiler, memory))
    report["digest"] = digest(report)
    return report
