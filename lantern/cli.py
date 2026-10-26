"""Talk to Lantern from the terminal.

  python -m lantern                       # chat mode
  python -m lantern "move my invoices into ~/Documents/Tax"
  python -m lantern --tasks               # what Lantern has done
  python -m lantern --task 3              # full timeline of task 3
  python -m lantern --memory              # what Lantern remembers about you
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

from . import config as config_mod
from .agent import PERSONAS, Agent
from .audit import AuditLog, GuardedClient
from .memory import Memory
from .policy import ZONES, Policy
from .router import Router, RouterConfig, make_backend
from .tools import Toolbox


def ask_approval(description: str) -> bool:
    try:
        answer = input(f"\n  ⚠  Lantern wants to: {description}\n     Allow? [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def show_event(kind: str, data: dict) -> None:
    if kind == "tool" and data["name"] != "finish":
        args = ", ".join(f"{k}={v!r}"[:60] for k, v in data["args"].items())
        mark = "✗" if "error" in data["result"] else "•"
        print(f"  {mark} {data['name']}({args})")


def fmt_time(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%b %d %H:%M")


def print_tasks(mem: Memory) -> None:
    tasks = mem.recent_tasks(20)
    if not tasks:
        print("No tasks yet.")
    for t in tasks:
        icon = {"done": "✓", "failed": "✗", "active": "…"}.get(t.status, "-")
        print(f"{icon} #{t.id} {fmt_time(t.created_at)}  {t.goal}")
        if t.summary:
            print(f"     → {t.summary}")
        u = mem.task_usage(t.id)
        if u["calls"]:
            print(f"     ${u['cost_usd']:.4f} · {u['input_tokens']} in ({u['cached_tokens']} cached) / {u['output_tokens']} out")


def print_usage(mem: Memory) -> None:
    rows = mem.usage_by_model()
    if not rows:
        print("No model calls logged yet.")
    for r in rows:
        print(f"{r['model']:<28} {r['calls']:>4} calls  {r['input_tokens']:>9} in"
              f" ({r['cached_tokens']} cached)  {r['output_tokens']:>8} out  ${r['cost_usd']:.4f}")
    print(f"Total spent: ${mem.total_cost():.4f}")


def print_audit(audit: AuditLog) -> None:
    s = audit.summary()
    c, loc = s["cloud"], s["local"]
    print(f"Today: {c['calls']} cloud calls, {c['bytes']:,} bytes left this laptop"
          f" · {loc['calls']} local calls ({loc['bytes']:,} bytes stayed here)")
    if s["selfhosted"]["calls"]:
        print(f"  Self-hosted private lane: {s['selfhosted']['calls']} calls, {s['selfhosted']['bytes']:,} bytes")
    for model, m in c["by_model"].items():
        print(f"  {model:<28} {m['calls']:>4} calls {m['bytes']:>10,} bytes")
    if c["redactions"]:
        print("  Redacted before sending: " + ", ".join(f"{k}×{v}" for k, v in c["redactions"].items()))


def print_task(mem: Memory, task_id: int) -> None:
    t = mem.get_task(task_id)
    u = mem.task_usage(task_id)
    print(f"#{t.id} [{t.status}] {t.goal}  (${u['cost_usd']:.4f}, {u['calls']} model calls)\n")
    for s in mem.steps(task_id):
        print(f"  {fmt_time(s['created_at'])}  {s['kind']:<11} {s['content'][:160]}")


def build_agent(cfg, mem: Memory) -> Agent:
    from openai import OpenAI
    key = os.environ.get("MODEL_API_KEY")
    if not key:
        sys.exit("Set MODEL_API_KEY first (your key from dev.meta.ai → API keys).")
    audit = AuditLog(cfg.audit_path)
    client = GuardedClient(OpenAI(base_url=cfg.base_url, api_key=key), audit, destination=cfg.base_url)
    policy = Policy.load(cfg.zones_file, cfg.roots)
    router = Router(policy, make_backend(RouterConfig.from_env(dict(os.environ)), audit))
    tools = Toolbox(policy, mem, ask_approval, router=router)
    return Agent(client, mem, tools, model=cfg.model, persona=cfg.persona, on_event=show_event)


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="lantern")
    p.add_argument("goal", nargs="*", help="what you want done")
    p.add_argument("--tasks", action="store_true", help="list recent tasks")
    p.add_argument("--task", type=int, help="show one task's full timeline")
    p.add_argument("--memory", action="store_true", help="show remembered facts")
    p.add_argument("--usage", action="store_true", help="show tokens and cost per model")
    p.add_argument("--zones", action="store_true", help="show privacy zones")
    p.add_argument("--audit", action="store_true", help="what left this laptop today")
    p.add_argument("--set-zone", nargs=2, metavar=("PATH", "ZONE"), help=f"set a folder's zone ({'/'.join(ZONES)})")
    p.add_argument("--persona", choices=sorted(PERSONAS))
    p.add_argument("--serve", action="store_true", help="run the local API server for the desktop app and channels")
    p.add_argument("--port", type=int, default=8765)
    a = p.parse_args(argv)

    cfg = config_mod.load()
    if a.persona:
        cfg.persona = a.persona
    mem = Memory(cfg.db_path)

    if a.tasks:
        return print_tasks(mem)
    if a.task:
        return print_task(mem, a.task)
    if a.usage:
        return print_usage(mem)
    if a.audit:
        return print_audit(AuditLog(cfg.audit_path))
    if a.zones or a.set_zone:
        policy = Policy.load(cfg.zones_file, cfg.roots)
        if a.set_zone:
            policy.set_zone(*a.set_zone)
            policy.save(cfg.zones_file)
        for z in policy.describe():
            print(f"{z['zone']:<9} {z['path']}")
        return
    if a.memory:
        facts = mem.facts()
        print("\n".join(f"- {k}: {v}" for k, v in facts.items()) or "Nothing remembered yet.")
        return

    if a.serve:
        from .core import Core
        from .server import serve
        mem.close()
        return serve(Core(cfg), config_mod.HOME, port=a.port)

    agent = build_agent(cfg, mem)

    def do(goal: str) -> None:
        r = agent.run(goal)
        print(f"\n🏮 {r.reply.strip()}\n")

    if a.goal:
        return do(" ".join(a.goal))

    print(f"🏮 Lantern ({cfg.persona}) watching: {', '.join(cfg.roots)}")
    print("   Type a task, /tasks, /memory, or /quit\n")
    while True:
        try:
            line = input("you › ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in ("/quit", "/exit"):
            break
        if line == "/tasks":
            print_tasks(mem)
            continue
        if line == "/memory":
            print("\n".join(f"- {k}: {v}" for k, v in mem.facts().items()) or "Nothing remembered yet.")
            continue
        try:
            do(line)
        except Exception as e:  # keep the chat alive on network/API errors
            print(f"  ✗ error: {e}")


if __name__ == "__main__":
    main()
