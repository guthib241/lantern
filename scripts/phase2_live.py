"""Check Phase 2 (S1 scheduler, S2 recovery, S3 ladder) against the LIVE Meta API, and report.

    python scripts/phase2_live.py                # every check
    python scripts/phase2_live.py --only 1 3     # just these (numbers from --list)
    python scripts/phase2_live.py --list
    python scripts/phase2_live.py --out report.json

Runs in a fresh rehearsal folder (a copy of demo/home with its own memory, zones and key; your real
~/.lantern is not touched). The model is the real one; email is the fake outbox, and the private lane is the
offline stand-in unless LANTERN_GLIMMER_URL is set. Approvals are tapped on the simulated phone.

It costs real money. Each task has a $0.25 limit (LANTERN_TASK_BUDGET_USD), the run stops starting new checks
once it has spent --max-usd (default $1.00), and the totals it prints are measured from the usage rows.
Never prints the key.

Checks:
  1  S1  three tasks at once: one asks a question (ask_user) and parks; the other two finish first;
         the answer arrives as "#<id> <answer>" on the phone and the parked one finishes with it
  2  S2  a tool error is fed back and the model takes another way
  3  S2  "send the same email twice": the second send is refused, the outbox has one email
  4  S3  the ladder: a planner turn on Spark at high effort, then a move to Wonder on a fresh chain (recap)
  5  S1  shutdown mid-task and a new process continues it from the checkpoint
  6  S4  the agent fills demo/portal (6 pages), Lantern learns it (Muse Spark writes the contract and the
         code, verified on a fresh portal), runs it again with no model calls, the portal switches to its v2
         layout, the routine heals (one Muse Spark call per moved field or button) and runs at $0 again.
         Needs Chromium (Playwright) for the browser.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lantern import demo, models  # noqa: E402
from lantern.ladder import Ladder, format_table  # noqa: E402

TASK_LIMIT = "0.25"


class Recorder:
    """Wraps the model client and keeps every request (after scrubbing) to check what was sent: the Responses
    API (the agent) and Chat Completions (the routine compiler's structured calls)."""

    def __init__(self, inner):
        self.inner, self.requests, self._lock = inner, [], threading.Lock()
        self.responses = self
        if hasattr(inner, "chat"):
            self.chat = type("Chat", (), {})()
            self.chat.completions = type("Completions", (), {"create": lambda _, **kw: self._chat(**kw)})()

    def _keep(self, kw: dict) -> None:
        with self._lock:
            self.requests.append(json.loads(json.dumps({k: v for k, v in kw.items() if k != "stream"}, default=str)))

    def create(self, **kw):
        self._keep(kw)
        return self.inner.responses.create(**kw)

    def _chat(self, **kw):
        self._keep(kw)
        return self.inner.chat.completions.create(**kw)


@dataclass
class Live:
    """One Lantern (Core + simulated phone) on the rehearsal folder, with every event kept."""
    core: Any
    hub: Any
    phone: Any
    events: list = field(default_factory=list)

    def task_of(self, goal: str) -> int | None:
        return next((e["task_id"] for e in self.events if e.get("type") == "queued" and goal in str(e.get("goal"))),
                    None)

    def done(self, task_id: int | None) -> dict | None:
        return next((e for e in self.events if e.get("type") == "done" and e.get("id") == task_id), None)

    def of(self, task_id: int | None, kind: str) -> list[dict]:
        return [e for e in self.events if e.get("type") == kind and e.get("task_id") == task_id]


def start(cfg, client, workers: int = 3) -> Live:
    core, hub, phone = demo.build(cfg, client=client, workers=workers)
    live = Live(core, hub, phone)
    core.subscribe(live.events.append)
    hub.question_delay = 0.5
    hub.start(listen=False)

    def tap(kind: str, a) -> None:   # the owner taps Approve on every card that reaches the phone
        if kind == "requested":
            button = f"approve:{a.id}:{a.nonce}:{a.payload_hash[:8]}"
            phone.note_owner(button=button)
            hub.receive(phone.incoming(button=button))
    core.bus.subscribe(tap)
    return live


def until(check: Callable[[], Any], timeout: float) -> Any:
    end = time.time() + timeout
    while time.time() < end:
        value = check()
        if value:
            return value
        time.sleep(0.2)
    return None


def say(live: Live, text: str) -> int | None:
    """Send `text` from the phone; returns the task it became."""
    before = len(live.events)
    live.hub.receive(live.phone.incoming(text=text))
    found = until(lambda: next((e["task_id"] for e in live.events[before:] if e.get("type") == "queued"), None), 30)
    return found


# ---- the checks ------------------------------------------------------------------------------
ASK = "Use ask_user to ask me which permit type I want: residential or commercial. Then reply with only my choice."
LOOK = "List the folders you can see and say which privacy zone each one is in."
TIDY = "Tidy my Downloads folder."


def check_three_at_once(cfg, client, timeout) -> dict:
    live = start(cfg, client)
    ask_id, look_id, tidy_id = say(live, ASK), say(live, LOOK), say(live, TIDY)
    parked = until(lambda: any(e.get("reason") == "waiting_user" for e in live.of(ask_id, "task_parked")), timeout)
    others = until(lambda: live.done(look_id) and live.done(tidy_id), timeout)
    still_parked = live.core.memory.get_task(ask_id).status == "active" and bool(live.core.questions())
    question = until(lambda: any("need" in m.get("text", "") and "answer" in m.get("text", "")
                                 for m in live.phone.messages()), 10)
    live.hub.receive(live.phone.incoming(text=f"#{ask_id} commercial"))
    answered = until(lambda: live.done(ask_id), timeout) or {}
    reply = str(answered.get("reply") or answered.get("summary") or "")
    ok = bool(parked and others and still_parked and answered.get("status") == "done")
    notes = [f"parked: {bool(parked)}", f"others finished while parked: {bool(others and still_parked)}",
             f"question reached the phone: {bool(question)}", f"reply mentions the answer: {'commercial' in reply.lower()}",
             f"juggling shown: {any(e.get('type') == 'juggling' and e.get('on') for e in live.events)}"]
    live.core.shutdown()
    live.hub.stop()
    return {"ok": ok, "notes": notes, "reply": reply[:300], "tasks": [ask_id, look_id, tidy_id]}


def check_tool_error(cfg, client, timeout) -> dict:
    live = start(cfg, client)
    goal = ("Read Documents/no-such-file.txt. If that fails, list the Documents folder instead and name the files "
            "in it.")
    tid = live.core.submit(goal)
    done = until(lambda: live.done(tid), timeout) or {}
    attempts = live.of(tid, "recovery")
    ok = done.get("status") == "done" and any(a.get("class") == "tool_error" for a in attempts)
    live.core.shutdown()
    return {"ok": ok, "notes": [f"recovery steps: {[(a.get('class'), a.get('strategy')) for a in attempts]}"],
            "reply": str(done.get("reply") or "")[:300], "tasks": [tid]}


def check_no_double_send(cfg, client, timeout) -> dict:
    live = start(cfg, client)
    goal = ("Email the organiser with subject 'Lantern live check' and body 'test 1'. Then send the same email "
            "again with the same subject and body 'test 2'.")
    tid = live.core.submit(goal)
    done = until(lambda: live.done(tid), timeout) or {}
    outbox = Path(cfg.media_dir) / "outbox"
    sent = len(list(outbox.glob("*.eml"))) if outbox.exists() else 0
    refused = [a for a in live.of(tid, "recovery") if a.get("strategy") == "refuse_duplicate"]
    tried_twice = sum(1 for e in live.of(tid, "tool") if e.get("name") == "send_email") >= 2
    ok = sent == 1 and (bool(refused) or not tried_twice)
    notes = [f"emails in the outbox: {sent}", f"model tried a second send: {tried_twice}",
             f"second send refused by recovery: {bool(refused)}"]
    if not tried_twice:
        notes.append("inconclusive for the guard: the model itself didn't try to send twice")
    live.core.shutdown()
    return {"ok": ok, "notes": notes, "reply": str(done.get("reply") or "")[:300], "tasks": [tid]}


def check_ladder(cfg, client, timeout) -> dict:
    live = start(cfg, client)
    lad = Ladder(live.core.memory)
    for _ in range(3):   # past failures at Spark-low: the first planner turn goes to Spark-high
        lad.record("plan", 3, False)
    goal = "List the files in my Downloads folder, then list my Documents folder, then say which folder has more files."
    tid = live.core.submit(goal)

    def bump() -> bool:   # after the first tool step, push this task's planner to Wonder (as model_confused would)
        if not live.of(tid, "tool"):
            return False
        run = live.core.scheduler.runs.get(tid)
        if run is not None and run.agent is not None and run.agent.ladder is not None:
            run.agent.ladder.floor["plan"] = 5
        return True
    until(bump, timeout)
    done = until(lambda: live.done(tid), timeout) or {}
    rungs = [e.get("rung") for e in live.of(tid, "usage")]
    wonder = [r for r in client.requests if r.get("model") == models.WONDER]
    high = [r for r in client.requests if (r.get("reasoning") or {}).get("effort") == "high"]
    fresh = bool(wonder) and "previous_response_id" not in wonder[0]
    ok = done.get("status") == "done" and 4 in rungs and 5 in rungs and fresh
    live.core.shutdown()
    return {"ok": ok, "notes": [f"rungs used: {rungs}", f"effort high accepted: {bool(high)}",
                                f"Wonder started a fresh chain with a recap: {fresh}"],
            "reply": str(done.get("reply") or "")[:300], "tasks": [tid]}


def check_restart(cfg, client, timeout) -> dict:
    live = start(cfg, client)
    goal = "List the folders you can see, then list the Documents folder, then summarise both in one line."
    tid = live.core.submit(goal)
    until(lambda: live.of(tid, "tool"), timeout)
    live.core.scheduler.shutdown()   # stops at the next turn and keeps its checkpoint
    until(lambda: not live.core.scheduler.runs, timeout)
    saved = live.core.memory.load_task_state(tid)
    live.hub.stop()
    second = start(cfg, client)      # a new process on the same memory
    done = until(lambda: second.done(tid), timeout) or {}
    notes = [n["content"] for n in second.core.memory.steps(tid) if n["kind"] == "note"]
    recap = any("no longer had" in n for n in notes)
    ok = saved is not None and done.get("status") == "done"
    second.core.shutdown()
    second.hub.stop()
    return {"ok": ok, "notes": [f"checkpoint saved: {saved is not None}",
                                "chain after restart: " + ("lost, continued with a recap" if recap else "kept")],
            "reply": str(done.get("reply") or "")[:300], "tasks": [tid]}


PORTAL = Path(__file__).resolve().parent.parent / "demo" / "portal"


def portal_server(layout: str = "v1"):
    sys.path.insert(0, str(PORTAL))
    try:
        from server import serve
    finally:
        sys.path.remove(str(PORTAL))
    httpd, app = serve(port=0, layout=layout, background=True)
    return f"http://127.0.0.1:{httpd.server_address[1]}", app, httpd


def check_routines(cfg, client, timeout) -> dict:
    base, app, httpd = portal_server("v1")
    servers = [httpd]
    plan = Path(cfg.roots[0]) / "Documents" / "site_plan.pdf"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_bytes((PORTAL / "fixtures" / "site_plan.pdf").read_bytes())
    live = start(cfg, client)

    def sandbox(_base: str) -> str:   # a fresh portal in the current layout, to verify a routine on
        url, _, h = portal_server(app.default_layout)
        servers.append(h)
        return url
    live.core.compiler.sandbox = sandbox
    goal = (f"Fill the city permit application on {base}. Use fill_form once per page, in order: "
            f"{base}/apply/personal, {base}/apply/address, {base}/apply/permit, {base}/apply/documents, "
            f"{base}/apply/review, {base}/apply/payment. Use my profile; date of birth 1994-03-12, address "
            "14 Lake View Road, Bengaluru, PIN code 560001; permit type home renovation, starting 2026-11-20 for "
            "14 days, the work: rebuild the front boundary wall; ID proof: passport; attach "
            f"{plan}; pay at the counter and accept the declaration.")
    notes, ok = [], True
    try:
        first = live.core.submit(goal)
        done = until(lambda: live.done(first), timeout * 2) or {}
        calls1 = live.core.memory.task_usage(first)["calls"]
        notes.append(f"1st run (agent): {done.get('status')}, {calls1} model calls, "
                     f"${live.core.memory.task_usage(first)['cost_usd']:.4f}")
        offered = any(e.get("type") == "learn_offer" and e.get("task_id") == first for e in live.events)
        notes.append(f"offered to learn it: {offered}")
        job = live.core.learn(first)
        learned = until(lambda: live.done(job), timeout) or {}
        notes.append(f"learn: {learned.get('status')}: {str(learned.get('summary'))[:160]}")
        rows = live.core.routines()
        if not rows or not rows[0]["trusted"]:
            raise RuntimeError("no trusted routine after learning")
        name = rows[0]["name"]
        runs = []
        for label in ("2nd run (routine, v1 layout)", "3rd run (portal switched to v2: heal)", "4th run (healed routine)"):
            if label.startswith("3rd"):
                app.default_layout = "v2"
            tid = live.core.submit(goal)
            r = until(lambda: live.done(tid), timeout * 2) or {}
            u = live.core.memory.task_usage(tid)
            runs.append((r.get("status"), u["calls"]))
            notes.append(f"{label}: {r.get('status')}, {u['calls']} model calls, ${u['cost_usd']:.4f}: "
                         f"{str(r.get('summary'))[:140]}")
        summary = live.core.compiler.store.summary(name)
        notes.append(f"routine {name}: v{summary['version']}, {summary['runs']} runs, {summary['heals']} heal(s), "
                     f"applications on the portal: {len(app.applications)}")
        ok = (done.get("status") == "done" and runs[0] == ("done", 0) and runs[1][0] == "done"
              and runs[2] == ("done", 0) and summary["heals"] >= 1)
    except Exception as e:
        ok = False
        notes.append(f"stopped: {type(e).__name__}: {e}")
    finally:
        live.core.shutdown()
        live.hub.stop()
        for h in servers:
            h.shutdown()
            h.server_close()
    return {"ok": ok, "notes": notes, "reply": "", "tasks": []}


CHECKS = [
    ("S1 three tasks at once, one parks for an answer", check_three_at_once),
    ("S2 a tool error, then another way", check_tool_error),
    ("S2 never sends the same email twice", check_no_double_send),
    ("S3 Spark high, then Wonder on a fresh chain", check_ladder),
    ("S1 shutdown mid-task, a new process continues it", check_restart),
    ("S4 learn the portal, rerun at $0, layout flip, heal, $0 again", check_routines),
]


def run(client, data: Path, picks: list[int], *, max_usd: float = 1.0, timeout: float = 240,
        out: Callable[[str], None] = print) -> list[dict]:
    os.environ.setdefault("LANTERN_TASK_BUDGET_USD", TASK_LIMIT)
    report: list[dict] = []
    spent = 0.0
    for n in picks:
        title, fn = CHECKS[n - 1]
        if spent >= max_usd:
            out(f"── {n}. {title}: skipped (the run spent ${spent:.4f} of its ${max_usd:.2f})")
            continue
        cfg = demo.prepare(data / f"check{n}")
        cfg.base_url = models.BASE_URL   # prepare() points the audit at rehearsal://; this run is live
        out(f"── {n}. {title}")
        t0 = time.time()
        try:
            r = fn(cfg, client, timeout)
        except Exception as e:   # one broken check must not hide the others
            r = {"ok": False, "notes": [f"crashed: {type(e).__name__}: {e}"], "reply": "", "tasks": []}
        from lantern.memory import Memory
        from lantern.crypto import load_cipher
        mem = Memory(cfg.db_path, load_cipher(Path(cfg.db_path).parent, "file") if cfg.encrypt else None)
        cost = mem.total_cost()
        table = format_table(Ladder(mem).table())
        mem.close()
        spent += cost
        r.update(n=n, title=title, seconds=round(time.time() - t0, 1), cost_usd=round(cost, 5))
        report.append(r)
        out(f"   {'PASS' if r['ok'] else 'FAIL'} · {r['seconds']} s · ${cost:.4f}")
        for note in r["notes"]:
            out(f"   • {note}")
        if r.get("reply"):
            out(f"   🏮 {r['reply']}")
        out("   ladder:\n" + "\n".join("     " + line for line in table.splitlines()) + "\n")
    out(f"Total measured: ${spent:.4f} · {sum(r['ok'] for r in report)}/{len(report)} passed")
    return report


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--only", nargs="*", type=int, help="check numbers to run")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--max-usd", type=float, default=1.0)
    ap.add_argument("--timeout", type=float, default=240, help="seconds per wait")
    ap.add_argument("--out", default="", help="write the JSON report here")
    a = ap.parse_args(argv)
    if a.list:
        for i, (title, _) in enumerate(CHECKS, 1):
            print(f"{i}. {title}")
        return 0
    if not os.environ.get(models.API_KEY_ENV):
        print(f"set {models.API_KEY_ENV} first")
        return 2
    from openai import OpenAI
    client = Recorder(OpenAI(base_url=models.BASE_URL, api_key=os.environ[models.API_KEY_ENV], timeout=180,
                             max_retries=0))
    data = Path(tempfile.mkdtemp(prefix="lantern-phase2-"))
    print(f"🏮 Phase 2 live checks in {data}\n")
    report = run(client, data, a.only or list(range(1, len(CHECKS) + 1)), max_usd=a.max_usd, timeout=a.timeout)
    if a.out:
        Path(a.out).write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    return 0 if all(r["ok"] for r in report) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
