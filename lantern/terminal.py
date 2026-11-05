"""Tasks typed in the terminal (`lantern "goal"` and the chat prompt) run on the same path as the desktop and phone.

Before Oct 28 the terminal ran a bare Agent: no Planner/Reader split, so a file's text (and any instructions hidden
in it) reached the model that chooses actions; only the approvals and the model's own judgement stood in the way
(live attack run, docs/DECISIONS.md). Now a terminal task goes through the scheduler with one worker, exactly as
`lantern --serve` runs it (security.py: handles, labels, risk tiers; recovery; the ladder; routines), and this module
is only the terminal's face for it:
  - approval cards are asked here, with their tier and flags; a flagged card (a recipient, link or command that came
    from content, or anything after untrusted content) needs the word "yes", not just "y";
  - the model's questions (ask_user) are asked here and answered on the same task;
  - Ctrl+C stops this task only ("stop #N"), and the prompt stays.
The terminal's Core never takes over tasks another Lantern left unfinished (recover=False): `lantern --resume` does.
"""
from __future__ import annotations

import queue
from typing import Any, Callable

Ask = Callable[[str], str]
Out = Callable[[str], None]


def _ask(ask: Ask, prompt: str) -> str:
    try:
        return ask(prompt)
    except EOFError:   # no terminal to answer: never approve by default
        return ""


class TerminalTasks:
    def __init__(self, core: Any, *, ask: Ask = input, out: Out = print):
        self.core, self.ask, self.out = core, ask, out
        self._events: queue.Queue = queue.Queue()
        core.subscribe(self._events.put)

    def run(self, goal: str) -> dict:
        """Run one goal to its end; returns the task's `done` event (status, reply, summary)."""
        return self.watch([self.core.submit(goal)])[0]

    def watch(self, task_ids: list[int]) -> list[dict]:
        """Answer these tasks' cards and questions here until each has ended; their `done` events, in order."""
        want, done, asked = set(task_ids), {}, set()
        try:
            while want - set(done):
                try:
                    e = self._events.get(timeout=0.2)
                except queue.Empty:
                    e = None
                if e is not None:
                    tid = e.get("id", e.get("task_id"))
                    if e.get("type") == "done" and tid in want:
                        done[tid] = e
                    else:
                        self._show(e, want)
                self._approvals(want, asked)
                self._questions(want, asked)
                if e is None:
                    self._ended_quietly(want, done)
        except KeyboardInterrupt:
            for tid in want - set(done):
                self.core.stop_task(tid)
                self.out(f"\n  🛑 stopping task #{tid}…")
            while want - set(done):   # wait for their ends, so the next prompt starts clean
                e = self._events.get()
                if e.get("type") == "done" and e.get("id", e.get("task_id")) in want:
                    done[e.get("id", e.get("task_id"))] = e
        return [done[t] for t in task_ids]

    def _ended_quietly(self, want: set[int], done: dict) -> None:
        """A task that ended before we were listening (recovered at start-up) has no `done` event for us."""
        for tid in want - set(done):
            t = self.core.memory.get_task(tid)
            sched = self.core.scheduler
            if t.status != "active" and (sched is None or tid not in sched.runs):
                done[tid] = {"type": "done", "id": tid, "task_id": tid, "status": t.status, "summary": t.summary,
                             "reply": t.summary}

    # ---- what the terminal shows -------------------------------------------------------------
    def _show(self, e: dict, want: set[int]) -> None:
        if e.get("task_id") is not None and e.get("task_id") not in want:
            return
        kind = e.get("type")
        if kind == "tool" and e.get("name") != "finish":
            args = ", ".join(f"{k}={v!r}"[:60] for k, v in (e.get("args") or {}).items())
            r = e.get("result")
            mark = "✗" if isinstance(r, dict) and r.get("error") else "•"
            self.out(f"  {mark} {e.get('name')}({args})  [{e.get('rung_badge') or e.get('badge') or ''}]")
        elif kind == "form" and e.get("phase") == "step":
            self.out(f"    [{e.get('badge')}] {e.get('action')}")
        elif kind == "retry":
            self.out(f"  … {e.get('error')} Retrying in {e.get('wait_s')} s")

    def _approvals(self, want: set[int], asked: set[str]) -> None:
        bus, sched = self.core.bus, self.core.scheduler
        for a in bus.pending():
            if a.id in asked or (sched is not None and sched.task_for_approval(a.id) not in want | {None}):
                continue
            asked.add(a.id)
            flagged = bool(a.tainted or a.flags)
            lines = [f"\n  ⚠  Lantern wants to: {a.summary}"]
            if a.tier:
                lines.append(f"     risk: {a.tier}")
            for f in a.flags or []:
                lines.append(f"     {f if str(f).startswith('⚠') else '⚠ ' + str(f)}")
            if a.tainted and not a.flags:
                lines.append("     ⚠ asked after reading content from files, pages or emails")
            self.out("\n".join(lines))
            word = _ask(self.ask, "     Type yes to allow: " if flagged else "     Allow? [y/N] ").strip().lower()
            ok = word == "yes" if flagged else word in ("y", "yes")
            try:
                bus.decide(a.id, a.nonce, ok, by="terminal")
            except Exception as err:   # expired or answered elsewhere meanwhile
                self.out(f"     (that card closed: {err})")

    def _questions(self, want: set[int], asked: set[str]) -> None:
        for q in self.core.questions():
            key = f"q{q['task_id']}:{q.get('since')}"   # the same question asked again is a new one
            if q["task_id"] not in want or key in asked:
                continue
            asked.add(key)
            opts = q.get("options") or []
            self.out(f"\n  ❓ {q['question']}" + (f"  ({' / '.join(map(str, opts))})" if opts else ""))
            text = _ask(self.ask, "  you › ").strip()
            self.core.answer(q["task_id"], text or "I'd rather not say; carry on without it.", by="terminal")
