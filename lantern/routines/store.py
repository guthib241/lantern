"""Routines on disk (S4): ~/.lantern/routines/<name>/v<N>/{contract.json, routine.py, verify.json} plus
<name>/meta.json (current version, trusted, run history, heals). Lantern's own data, not user files, so it
is read and written here directly (like the widgets folder). Deleting one (forget) goes through
policy.erase_lantern_data like every other forget (Core.forget_routine).
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .contract import NAME

MAX_RUNS = 200   # run history kept per routine (for the chart and the totals)


@dataclass
class Routine:
    name: str
    version: int
    contract: dict
    code: str
    meta: dict

    @property
    def trusted(self) -> bool:
        return bool(self.meta.get("trusted"))


class RoutineStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser()

    def _dir(self, name: str) -> Path:
        if not NAME.match(name or ""):
            raise KeyError(f"no routine {name!r}")
        return self.root / name

    def _write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    def names(self) -> list[str]:
        return sorted(p.parent.name for p in self.root.glob("*/meta.json") if NAME.match(p.parent.name))

    def meta(self, name: str) -> dict:
        path = self._dir(name) / "meta.json"
        if not path.exists():
            raise KeyError(f"no routine {name!r}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _save_meta(self, meta: dict) -> None:
        self._write(self._dir(meta["name"]) / "meta.json", json.dumps(meta, indent=1, ensure_ascii=False))

    def load(self, name: str, version: int | None = None) -> Routine:
        meta = self.meta(name)
        v = int(version or meta["current"])
        d = self._dir(name) / f"v{v}"
        return Routine(name, v, json.loads((d / "contract.json").read_text(encoding="utf-8")),
                       (d / "routine.py").read_text(encoding="utf-8"), meta)

    def all(self) -> list[Routine]:
        out = []
        for n in self.names():
            try:
                out.append(self.load(n))
            except (OSError, ValueError, KeyError):
                continue   # a half-written routine is skipped, never run
        return out

    def save(self, contract: dict, code: str, verify: dict, *, how: str, trusted: bool,
             healed_from: int | None = None, patches: dict | None = None) -> Routine:
        """A new version; it becomes current only if trusted (or if there is no trusted one yet)."""
        name = contract["name"]
        try:
            meta = self.meta(name)
        except KeyError:
            meta = {"name": name, "title": contract.get("title", name), "kind": contract.get("kind"),
                    "created_at": time.time(), "current": 0, "trusted": False, "versions": [], "runs": [],
                    "heals": [], "source": contract.get("source", {})}
        v = max([x["v"] for x in meta["versions"]], default=0) + 1
        d = self._dir(name) / f"v{v}"
        self._write(d / "contract.json", json.dumps(contract, indent=1, ensure_ascii=False))
        self._write(d / "routine.py", code)
        self._write(d / "verify.json", json.dumps(verify, indent=1, ensure_ascii=False, default=str))
        meta["versions"].append({"v": v, "at": time.time(), "how": how, "trusted": trusted,
                                 "verify": {k: verify.get(k) for k in ("mode", "ok", "steps", "stopped_at", "note")}})
        if trusted or not meta["trusted"]:
            meta["current"], meta["trusted"] = v, trusted
            meta["title"], meta["examples"] = contract.get("title", name), contract.get("examples", [])
        if healed_from is not None:
            meta["heals"].append({"at": time.time(), "from": healed_from, "to": v, "patches": patches or {},
                                  "trusted": trusted})
        self._save_meta(meta)
        return self.load(name, v)

    def record_run(self, name: str, run: dict) -> None:
        meta = self.meta(name)
        meta["runs"] = (meta.get("runs") or []) + [{"at": time.time(), **run}]
        meta["runs"] = meta["runs"][-MAX_RUNS:]
        self._save_meta(meta)

    def add_example(self, name: str, example: str) -> None:
        meta = self.meta(name)
        ex = meta.get("examples") or []
        if example and example not in ex:
            meta["examples"] = (ex + [example])[-10:]
            self._save_meta(meta)

    def summary(self, name: str) -> dict:
        """For lists and the chart: measured numbers only (costs from the usage rows of real runs)."""
        m = self.meta(name)
        runs = m.get("runs") or []
        base = float((m.get("source") or {}).get("cost_usd") or 0.0)
        return {"name": name, "title": m.get("title"), "kind": m.get("kind"), "version": m.get("current"),
                "trusted": m.get("trusted"), "examples": m.get("examples", []), "runs": len(runs),
                "ok_runs": sum(1 for r in runs if r.get("ok")),
                "learned_from": (m.get("source") or {}).get("task_id"), "learned_cost_usd": round(base, 6),
                "saved_usd": round(sum(max(0.0, base - float(r.get("cost_usd") or 0)) for r in runs if r.get("ok")), 6),
                "last_healed": (m.get("heals") or [{}])[-1].get("at") if m.get("heals") else None,
                "heals": len(m.get("heals") or []),
                "history": [{"at": r["at"], "cost_usd": r.get("cost_usd", 0), "seconds": r.get("seconds", 0),
                             "version": r.get("version"), "ok": r.get("ok"), "healed": bool(r.get("healed"))}
                            for r in runs[-30:]]}

    def path(self, name: str) -> Path:
        """Where a routine lives (for forgetting it through policy.erase_lantern_data)."""
        return self._dir(name)


def valid_name(name: str) -> bool:
    return bool(re.match(NAME.pattern, name or ""))
