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
from pathlib import Path

from . import config as config_mod
from .agent import PERSONAS, Agent
from .audit import AuditLog, GuardedClient
from .crypto import cipher_for
from .email_tool import EmailService, EmailSettings
from .envmap import EnvMap
from .forms import FormService, FormSettings, Vault, VaultError
from .memory import Memory
from .policy import ZONES, Policy
from .router import Router, RouterConfig, make_backend
from .tools import Toolbox
from .widgets import Widgets


def ask_approval(description: str) -> bool:
    try:
        answer = input(f"\n  ⚠  Lantern wants to: {description}\n     Allow? [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def show_event(kind: str, data: dict) -> None:
    if kind == "form" and data.get("phase") == "step":
        print(f"    [{data.get('badge')}] {data.get('action')}")
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
    audit = AuditLog(cfg.audit_path, mem.cipher)
    client = GuardedClient(OpenAI(base_url=cfg.base_url, api_key=key), audit, destination=cfg.base_url)
    policy = Policy.load(cfg.zones_file, cfg.roots)
    router = Router(policy, make_backend(RouterConfig.from_env(dict(os.environ)), audit))
    forms = FormService(FormSettings.from_config(cfg), client, policy, mem)
    email = EmailService(EmailSettings.from_env(dict(os.environ)), policy, audit,
                         outbox_dir=os.path.join(cfg.media_dir, "outbox"))
    tools = Toolbox(policy, mem, ask_approval, router=router, forms=forms, email=email,
                    envmap=EnvMap(policy, cfg.envmap_path), widgets=Widgets(Path(cfg.widgets_dir)))
    tools.client = client
    tools.on_event = show_event
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
    p.add_argument("--profile", action="store_true", help="show the form profile vault")
    p.add_argument("--profile-set", nargs=2, metavar=("KEY", "VALUE"), help="save a profile answer for forms")
    p.add_argument("--form-bookmark", nargs=2, metavar=("NAME", "URL"), help="save a form's URL under a name")
    p.add_argument("--projects", action="store_true", help="list the projects Lantern found")
    p.add_argument("--which", metavar="PACKAGE", help="which projects use PACKAGE (no model call)")
    p.add_argument("--persona", choices=sorted(PERSONAS))
    p.add_argument("--serve", action="store_true", help="run the local API server for the desktop app and channels")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--channel-test", metavar="TEXT", help="send TEXT through the configured channels and exit")
    p.add_argument("--mcp", action="store_true", help="run Lantern as an MCP server on stdio")
    p.add_argument("--forget-task", type=int, metavar="ID", help="really delete one task, its timeline and audit rows")
    p.add_argument("--forget-fact", metavar="KEY", help="really delete one remembered fact")
    p.add_argument("--forget-everything", action="store_true", help="erase memory, audit log, voice notes, screenshots")
    a = p.parse_args(argv)

    cfg = config_mod.load()
    if a.persona:
        cfg.persona = a.persona
    mem = Memory(cfg.db_path, cipher_for(cfg))

    if a.tasks:
        return print_tasks(mem)
    if a.task:
        return print_task(mem, a.task)
    if a.usage:
        return print_usage(mem)
    if a.audit:
        return print_audit(AuditLog(cfg.audit_path, mem.cipher))
    if a.forget_task:
        counts = mem.forget_task(a.forget_task)
        AuditLog(cfg.audit_path, mem.cipher).forget_task(a.forget_task)
        return print(f"Forgot task #{a.forget_task}: {counts}")
    if a.forget_fact:
        return print("Forgot it." if mem.forget(a.forget_fact) else "Nothing remembered under that name.")
    if a.forget_everything:
        if input("Erase Lantern's memory, audit log, voice notes, screenshots and form profile? Type 'forget everything': ") \
                .strip().lower() != "forget everything":
            return print("Nothing erased.")
        from .core import Core
        mem.close()
        core = Core(cfg)
        print(core.forget_everything())
        return core.shutdown()
    if a.zones or a.set_zone:
        policy = Policy.load(cfg.zones_file, cfg.roots)
        if a.set_zone:
            policy.set_zone(*a.set_zone)
            policy.save(cfg.zones_file)
        for z in policy.describe():
            print(f"{z['zone']:<9} {z['path']}")
        return
    if a.projects or a.which:
        em = EnvMap(Policy.load(cfg.zones_file, cfg.roots), cfg.envmap_path)
        if a.which:
            r = em.which_use(a.which)
            for h in r["projects"]:
                inst = ", ".join(f"{v} in {Path(k).name}" for k, v in (h["installed"] or {}).items())
                print(f"• {h['project']:<24} declared {h['declared'] or '-':<12} installed {inst or '-'}  ({h['path']})")
            for c in r["conda_envs"]:
                print(f"• conda env {c['env']}: {c['version']}")
            print(f"{len(r['projects'])} of {r['scanned_projects']} projects use {a.which}")
        else:
            for p_ in em.projects(refresh=True):
                print(f"• {p_.name:<24} {'/'.join(p_.kinds) or '?':<14} {len(p_.declared):>3} deps  {p_.path}")
            print(f"({em.parsed} re-read, the rest from cache)")
        return
    if a.profile or a.profile_set or a.form_bookmark:
        vault = Vault(cfg.profile_path)
        try:
            if a.profile_set:
                vault.set(*a.profile_set)
            if a.form_bookmark:
                vault.set(*a.form_bookmark, section="forms")
        except VaultError as e:
            sys.exit(f"✗ {e}")
        data = vault.load()
        for section in ("profile", "answers", "forms"):
            for k, v in data[section].items():
                print(f"{section:<8} {k}: {v}")
        return
    if a.memory:
        facts = mem.facts()
        print("\n".join(f"- {k}: {v}" for k, v in facts.items()) or "Nothing remembered yet.")
        return

    if a.mcp:  # stdout belongs to the MCP protocol from here on: no prints
        from .mcp_server import LocalLantern, build_server
        from .server import load_token
        policy = Policy.load(cfg.zones_file, cfg.roots)
        audit = AuditLog(cfg.audit_path, mem.cipher)
        router = Router(policy, make_backend(RouterConfig.from_env(dict(os.environ)), audit))
        tools = Toolbox(policy, mem, lambda s: False, router=router, envmap=EnvMap(policy, cfg.envmap_path))
        local = LocalLantern(os.environ.get("LANTERN_API", "http://127.0.0.1:8765"), load_token(config_mod.HOME))
        return build_server(tools, audit=audit, forward=local.request).run("stdio")
    if a.serve or a.channel_test:
        from .channels import ChannelHub, channels_from_env
        from .core import Core
        from .server import serve
        mem.close()
        core = Core(cfg)
        hub = ChannelHub(core, channels_from_env(dict(os.environ)))
        if a.channel_test:  # prove the phone side works before the demo
            for ch in hub.channels.values():
                ch.send_text(a.channel_test)
                print(f"sent via {ch.name}")
            return
        return serve(core, config_mod.HOME, port=a.port, hub=hub.start())

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
