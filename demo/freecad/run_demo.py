"""Play the three S17 demo tasks through Lantern's FreeCAD connector and check every result.

    python demo/freecad/run_demo.py                 # fake geometry, numbers only (free, any machine)
    python demo/freecad/run_demo.py --spark         # + Muse Spark's visual check of each render (MODEL_API_KEY)
    python demo/freecad/run_demo.py --freecad       # real FreeCAD (laptop; FreeCADCmd on PATH or LANTERN_FREECAD_CMD)
    python demo/freecad/run_demo.py --only phone_stand

Each task runs its scripted plan (the calls Muse Spark is expected to make; tasks.json) through the
real MCP server and Lantern's MCP client, with approvals auto-tapped "yes", then checks the expected
numbers (and the picture with --spark). Exports go to a fresh folder that is the only writable zone.
Prints one line per check and exits non-zero if any task fails.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from lantern.integrations.freecad import tiers  # noqa: E402
from lantern.integrations.freecad.backend import make_backend  # noqa: E402
from lantern.integrations.freecad.connector import FreeCADConnector  # noqa: E402
from lantern.integrations.freecad.server import Session, build_server  # noqa: E402
from lantern.mcp_client import MCPHub  # noqa: E402
from lantern.memory import Memory  # noqa: E402
from lantern.policy import Policy  # noqa: E402
from lantern.tools import Toolbox  # noqa: E402

TASKS = Path(__file__).with_name("tasks.json")


def load_tasks() -> list[dict]:
    return json.loads(TASKS.read_text(encoding="utf-8"))["tasks"]


def fill(value, out: Path):
    if isinstance(value, str):
        return value.replace("{out}", str(out))
    if isinstance(value, list):
        return [fill(v, out) for v in value]
    if isinstance(value, dict):
        return {k: fill(v, out) for k, v in value.items()}
    return value


def same_path(a: str | None, b: str) -> bool:
    """Equal as file paths: on Windows 'out/x.stl' and 'out\\x.stl' (any case) are the same file."""
    return bool(a) and os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


class Run:
    """One connected session (tasks that continue a previous one share it, like the third demo)."""

    def __init__(self, backend: str, out: Path, client=None):
        self.out = out
        self.policy = Policy.from_roots([out])
        self.session = Session(make_backend(backend), self.policy, out / ".renders")
        self.hub = MCPHub(servers={"freecad": build_server(self.session)}).start()
        if self.hub.errors:
            raise SystemExit(f"MCP server didn't start: {self.hub.errors}")
        for fn, ref in self.hub.tools.items():
            ref.read_only = ref.tool in tiers.GREEN_TOOLS
        self.memory = Memory(out / ".demo-memory.db")
        self.approvals: list[str] = []
        self.toolbox = Toolbox(self.policy, self.memory, lambda s: self.approvals.append(s) or True, mcp=self.hub)
        self.connector = FreeCADConnector(self.policy, client, self.memory, visual="on_check")

    def task(self, t: dict) -> dict:
        self.toolbox.task_id = self.memory.start_task(t["goal"]).id
        state = self.connector._state(self.toolbox)
        state.expect = t["expect"]   # scripted demo: the expected numbers are known (Spark derives them live)
        last = {}
        for op, args in t["plan"]:
            last = self.connector.call(self.toolbox, f"freecad.{op}", fill(args, self.out))
            if last.get("error") or last.get("done") is False:
                return {"passed": False, "failed": [f"{op}: {last.get('error') or last.get('reason')}"], "last": last}
        check = state.last_verdict or {}
        failed = list(check.get("failed", []))
        for f in fill(t.get("files", []), self.out):
            if not (same_path(last.get("path"), f) and last.get("verified")):
                failed.append(f"file {f} not written and verified")
        return {"passed": bool(check.get("passed")) and not failed, "failed": failed, "check": check, "last": last}

    def close(self) -> None:
        self.hub.close()
        self.session.backend.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--freecad", action="store_true", help="real FreeCAD instead of the fake geometry")
    ap.add_argument("--spark", action="store_true", help="add Muse Spark's visual check (uses MODEL_API_KEY)")
    ap.add_argument("--only", nargs="*", help="task ids")
    ap.add_argument("--out", default="", help="folder for exports and renders (default: a new temp folder)")
    a = ap.parse_args(argv)
    client = None
    if a.spark:
        from lantern.config import load_env
        load_env()   # the repo's .env (gitignored), like Lantern itself
        if not os.environ.get("MODEL_API_KEY"):
            raise SystemExit("--spark needs MODEL_API_KEY")
        from openai import OpenAI
        from lantern import models
        client = OpenAI(base_url=models.BASE_URL, api_key=os.environ["MODEL_API_KEY"])
    out = Path(a.out or tempfile.mkdtemp(prefix="lantern-freecad-demo-")).resolve()
    out.mkdir(parents=True, exist_ok=True)
    tasks = [t for t in load_tasks() if not a.only or t["id"] in a.only]
    runs: dict[str, Run] = {}
    ok = True
    print(f"🏮 FreeCAD demo · backend {'freecad' if a.freecad else 'fake'} · visual check {'Muse Spark' if a.spark else 'off'} · {out}\n")
    try:
        for t in tasks:
            run = runs.get(t.get("after", "")) or Run("freecad" if a.freecad else "fake", out, client)
            if t.get("after") and t["after"] not in runs:   # e.g. --only taller_export: build the enclosure first
                base = next(x for x in load_tasks() if x["id"] == t["after"])
                run.task(base)
            runs[t["id"]] = run
            r = run.task(t)
            ok &= r["passed"]
            print(f"{'✓' if r['passed'] else '✗'} {t['id']}: {t['goal']}")
            for c in (r.get("check") or {}).get("numeric", []):
                print(f"    {'✓' if c['passed'] else '✗'} {c['detail']}")
            vis = (r.get("check") or {}).get("visual")
            if vis:
                print("    eyes: " + ", ".join(f"{c['name']}={c['verdict']}" for c in vis.get("checks", [])))
            for f in r["failed"]:
                print(f"    ✗ {f}")
            if r["last"].get("path"):
                print(f"    file: {r['last']['path']} ({r['last'].get('bytes')} bytes, verified={r['last'].get('verified')})")
        renders = sorted((out / ".renders").glob("*.png"))
        print(f"\n{len(renders)} renders in {out / '.renders'}")
    finally:
        for run in {id(r): r for r in runs.values()}.values():
            run.close()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
