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
import json
import os
import sys
from pathlib import Path

from . import config as config_mod
from .agent import PERSONAS, Agent
from .audit import AuditLog, GuardedClient
from .budget import BudgetGuard, Limits
from .crypto import cipher_for
from .reliability import RetryingClient, explain
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
    guarded = GuardedClient(OpenAI(base_url=cfg.base_url, api_key=key, timeout=cfg.timeout, max_retries=0), audit,
                            destination=cfg.base_url)
    budget = BudgetGuard(mem, Limits.from_env(dict(os.environ)), lambda: getattr(guarded, "task_id", None),
                         lambda e: print(f"  … spent ${e['spent_usd']:.2f} of the ${e['limit_usd']:.2f} limit"))
    client = RetryingClient(guarded, on_retry=lambda info: print(f"  … {info['error']} Retrying in {info['wait_s']} s"),
                            before_call=budget.check)
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


def server_running(port: int) -> bool:
    """Is something (normally another Lantern) already listening on 127.0.0.1:port?"""
    import socket
    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def refuse_second_server(port: int) -> None:
    if server_running(port):
        sys.exit(f"Lantern is already running on http://127.0.0.1:{port}. Stop it first, or pick another --port.")


def resume_tasks(cfg, mem: Memory, port: int) -> None:
    """Ask the running server to resume, or (no server) run the cut-short tasks right here."""
    from .core import recover_interrupted, resume_goal
    if server_running(port):
        import urllib.request
        from .server import load_token
        req = urllib.request.Request(f"http://127.0.0.1:{port}/tasks/resume", method="POST", data=b"",
                                     headers={"X-Lantern-Token": load_token(config_mod.HOME)})
        with urllib.request.urlopen(req, timeout=10) as r:
            ids = json.loads(r.read())["task_ids"]
        return print(f"Asked the running Lantern to resume {len(ids)} task(s): {ids}")
    recover_interrupted(mem)
    tasks = mem.tasks_with_status("interrupted")
    if not tasks:
        return print("Nothing to resume.")
    agent = build_agent(cfg, mem)
    for t in tasks:
        goal = resume_goal(mem, t)
        mem.finish_task(t.id, "resumed", "resumed from the command line")
        print(f"\n▶ resuming #{t.id}: {t.goal.splitlines()[0]}")
        try:
            r = agent.run(goal)
            print(f"🏮 {r.reply.strip()}")
        except Exception as e:
            print(f"  ✗ {explain(e)}")


def run_demo(a) -> None:
    """Rehearsal: a fresh copy of demo/home, a scripted model, the simulated phone. Nothing leaves the laptop."""
    from . import demo
    if a.demo and not a.demo_run:
        refuse_second_server(a.port)
    cfg = demo.prepare(a.demo_dir)
    if a.persona:
        cfg.persona = a.persona
    core, hub, phone = demo.build(cfg)
    print(f"🏮 Rehearsal mode: scripted model, zero tokens, nothing sent. Data in {Path(cfg.db_path).parent}")
    if a.demo_run:
        hub.start(listen=False)
        try:
            result = demo.run_story(core, hub, phone)
        finally:
            hub.stop()
            core.shutdown()
        if any(b["status"] != "done" for b in result["beats"]):
            sys.exit("✗ some beats did not finish; see above")
        return
    from .server import serve
    print("   Try: " + " · ".join(b.goal for b in demo.STORY + demo.EXTRAS))
    return serve(core, config_mod.HOME, port=a.port, hub=hub.start())


def safe_console() -> None:
    """Lantern prints ✓, ⏰ and 🏮. A Windows console piped to a file or another program uses cp1252, where
    those would crash the print; show '?' instead and keep going."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")


def teach(cfg, mem: Memory, url: str, goal: str) -> None:
    """S8 from the terminal: Lantern's browser opens on URL; the user does TASK once; Enter ends the recording."""
    from .core import Core
    mem.close()
    core = Core(cfg, workers=max(1, cfg.workers))
    try:
        try:
            task_id = core.teach(url, goal)
        except ValueError as e:
            sys.exit(f"✗ {e}")
        print("🏮 Lantern's browser is opening. Do the task once, wait for its confirmation page, "
              "then press Enter here (Ctrl+C to throw the recording away).")
        try:
            input()
        except KeyboardInterrupt:
            core.stop()
        core.teach_stop(task_id)
        core.wait_idle(600)
        print(core.memory.get_task(task_id).summary)
        learned = [t for t in core.memory.recent_tasks(5) if t.goal.startswith(f"Learn task #{task_id} ")]
        if learned:
            print(learned[0].summary)
    finally:
        core.shutdown()


def main(argv=None) -> None:
    safe_console()
    p = argparse.ArgumentParser(prog="lantern")
    p.add_argument("goal", nargs="*", help="what you want done")
    p.add_argument("--tasks", action="store_true", help="list recent tasks")
    p.add_argument("--task", type=int, help="show one task's full timeline")
    p.add_argument("--receipt", type=int, metavar="TASK", help="a task's receipt: changes, checks, approvals, data sent, cost")
    p.add_argument("--memory", action="store_true", help="show remembered facts")
    p.add_argument("--usage", action="store_true", help="show tokens and cost per model")
    p.add_argument("--router", action="store_true", help="the model ladder: kind x rung -> success, cost, time")
    p.add_argument("--routines", action="store_true", help="learned routines: version, runs, money saved, last healed")
    p.add_argument("--sleep", action="store_true",
                   help="check and repair every learned routine now (nothing is sent or submitted), then the digest")
    p.add_argument("--teach", nargs=2, metavar=("URL", "TASK"),
                   help="show Lantern a web task once in its browser; it learns it as a routine")
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
    p.add_argument("--resume", action="store_true", help="continue the tasks cut short when Lantern last stopped")
    p.add_argument("--doctor", action="store_true", help="check everything Lantern needs on this computer")
    p.add_argument("--demo", action="store_true", help="with --serve: rehearsal mode (scripted model, demo/home, nothing sent)")
    p.add_argument("--demo-run", action="store_true", help="play the 3-minute demo story offline and print it")
    p.add_argument("--demo-dir", metavar="DIR", help="empty folder for the rehearsal's data (default: a new temp folder)")
    a = p.parse_args(argv)

    if a.demo or a.demo_run:
        return run_demo(a)

    cfg = config_mod.load()
    if a.persona:
        cfg.persona = a.persona
    if a.doctor:  # before opening memory: the doctor creates nothing
        from .doctor import report, run_checks
        print("🏮 Lantern doctor\n")
        print(report(run_checks(cfg)))
        return
    mem = Memory(cfg.db_path, cipher_for(cfg))

    if a.tasks:
        return print_tasks(mem)
    if a.task:
        return print_task(mem, a.task)
    if a.receipt:
        from .receipt import as_text, build
        try:
            return print(as_text(build(mem, AuditLog(cfg.audit_path, mem.cipher), a.receipt)))
        except KeyError:
            sys.exit(f"No task #{a.receipt}.")
    if a.usage:
        return print_usage(mem)
    if a.router:
        from .ladder import Ladder, format_table
        return print(format_table(Ladder(mem).table()))
    if a.routines:
        from .compiler import format_routines
        from .routines.store import RoutineStore
        store = RoutineStore(Path(cfg.db_path).expanduser().parent / "routines")
        return print(format_routines([store.summary(n) for n in store.names()]))
    if a.teach:
        return teach(cfg, mem, *a.teach)
    if a.sleep:
        from .core import Core
        mem.close()
        core = Core(cfg, workers=1)
        try:
            task_id = core.sleep()
            core.wait_idle(1800)
            print(core.memory.get_task(task_id).summary)
        finally:
            core.shutdown()
        return
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
    if a.resume:
        return resume_tasks(cfg, mem, a.port)
    if a.serve or a.channel_test:
        from .channels import ChannelHub, channels_from_env
        from .core import Core
        from .server import serve
        if a.serve:
            refuse_second_server(a.port)
            os.environ.setdefault("LANTERN_SLEEP_AT", "03:30")   # S11: the nightly check (sleep.py), "off" to stop
        mem.close()
        core = Core(cfg, workers=cfg.workers if a.serve else 0)
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
            print(f"  ✗ {explain(e)}")


if __name__ == "__main__":
    main()
