"""Deploy the private Glimmer server, test it, measure it, stop it.

    python deploy/modal/smoke.py                       # A10, Q4_K_M, DFlash drafter (the defaults)
    python deploy/modal/smoke.py --gpu A10 --no-draft  # try another setup
    python deploy/modal/smoke.py --keep                # leave it deployed (it still scales to zero)

Steps: deploy -> the gate refuses no token / a wrong token / an unknown path (no GPU
starts) -> cold start (first authorised request) -> one short prompt -> one tool call
and its follow-up -> a longer answer for decode speed -> canary check on the app logs
-> `modal app stop`. Prints a JSON report with timings, tokens/sec and the cost
estimate. The token is read from ~/.lantern/private_token and never printed.

The requests go through the OpenAI SDK with the same parameters as Lantern's router
(lantern/router.py OpenAIGlimmer), so passing here means the router will work.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import glimmer_config as gc  # noqa: E402

APP_FILE = HERE / "glimmer_app.py"
ROUTER_PARAMS = dict(temperature=1.0, top_p=0.95,
                     extra_body={"top_k": 64, "chat_template_kwargs": {"reasoning_strength": "low"}})
TOOLS = [{
    "type": "function",
    "function": {
        "name": "get_local_time",
        "description": "Current local time in a city.",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
    },
}]


def modal_cli(*args: str, env: dict | None = None, timeout: int = 3600) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "modal", *args], capture_output=True, text=True,
                          env={**os.environ, **(env or {})}, timeout=timeout)


def read_token(path: Path) -> str:
    try:
        return gc.validate_token(path.read_text().strip())
    except FileNotFoundError:
        raise SystemExit(f"no token at {path}; run deploy/modal/rotate_token.py first") from None


def timings_of(resp) -> dict:
    extra = getattr(resp, "model_extra", None) or {}
    t = extra.get("timings") or {}
    keep = ("prompt_n", "prompt_per_second", "predicted_n", "predicted_per_second", "draft_n", "draft_n_accepted")
    return {k: round(t[k], 2) if isinstance(t[k], float) else t[k] for k in keep if k in t}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Deploy, test and stop the private Glimmer server.")
    ap.add_argument("--gpu", default=None)
    ap.add_argument("--quant", default=None)
    ap.add_argument("--no-draft", action="store_true")
    ap.add_argument("--token-file", default=gc.DEFAULT_TOKEN_FILE)
    ap.add_argument("--skip-deploy", action="store_true", help="test what's deployed now")
    ap.add_argument("--keep", action="store_true", help="don't stop the app at the end")
    ap.add_argument("--report", default="", help="also write the JSON report to this file")
    args = ap.parse_args(argv)

    import httpx
    import modal
    from openai import OpenAI

    env = dict(os.environ)
    if args.gpu:
        env["LANTERN_GLIMMER_GPU"] = args.gpu
    if args.quant:
        env["LANTERN_GLIMMER_QUANT"] = args.quant
    if args.no_draft:
        env["LANTERN_GLIMMER_DRAFT"] = "0"
    cfg = gc.ServeConfig.from_env(env)
    token = read_token(Path(args.token_file).expanduser())
    report: dict = {"config": cfg.as_env(), "price_per_hour_usd": round(cfg.price_per_hour(), 4), "checks": {}}
    checks = report["checks"]

    def step(msg: str) -> None:
        print(f"[smoke] {msg}", file=sys.stderr, flush=True)

    if not args.skip_deploy:
        step(f"deploying {cfg} (first deploy downloads ~{sum(f.size_bytes for f in cfg.files()) / 1e9:.0f} GB once)")
        t = time.monotonic()
        r = modal_cli("deploy", str(APP_FILE), env=cfg.as_env())
        report["deploy_s"] = round(time.monotonic() - t, 1)
        if r.returncode:
            print(r.stdout[-3000:], r.stderr[-3000:], file=sys.stderr)
            raise SystemExit("modal deploy failed")
    url = modal.Function.from_name(gc.APP_NAME, "gate").get_web_url().rstrip("/")
    step(f"gate at {url}")

    http = httpx.Client(timeout=900, follow_redirects=True)
    probe = {"model": "muse-glimmer", "messages": [{"role": "user", "content": "hi"}]}
    for name, headers, path in [("no_token", {}, "/v1/chat/completions"),
                                ("wrong_token", {"Authorization": f"Bearer {secrets.token_urlsafe(32)}"},
                                 "/v1/chat/completions"),
                                ("basic_auth", {"Authorization": "Basic Zm9vOmJhcg=="}, "/v1/chat/completions"),
                                ("unknown_path", {"Authorization": f"Bearer {token}"}, "/slots")]:
        t = time.monotonic()
        resp = http.post(url + path, json=probe, headers=headers)
        want = 404 if name == "unknown_path" else 401
        checks[name] = {"status": resp.status_code, "ok": resp.status_code == want,
                        "ms": round((time.monotonic() - t) * 1000)}
        step(f"{name}: {resp.status_code}")

    gpu_t0 = time.monotonic()
    try:
        # Cold start: the first authorised request starts the GPU container and waits for llama-server.
        resp = http.get(url + "/v1/models", headers={"Authorization": f"Bearer {token}"})
        report["cold_start_s"] = round(time.monotonic() - gpu_t0, 1)
        ids = [m.get("id") for m in resp.json().get("data", [])] if resp.status_code == 200 else []
        checks["models"] = {"status": resp.status_code, "ok": gc.SERVED_MODEL in ids, "ids": ids}
        if resp.status_code == 401:
            raise RuntimeError(f"the gate rejected the token in {args.token_file}: it doesn't match the Modal "
                               "Secret (rotated on another machine?). rotate_token.py --redeploy here fixes it, "
                               "and makes other machines' token files stale")
        step(f"cold start {report['cold_start_s']} s, models {ids}")

        client = OpenAI(base_url=url + "/v1", api_key=token, timeout=900, max_retries=0)
        canary = f"lanterncanary{secrets.token_hex(4)}"

        t = time.monotonic()
        r = client.chat.completions.create(
            model=gc.SERVED_MODEL, max_tokens=1024, **ROUTER_PARAMS,
            messages=[{"role": "system", "content": "Answer in one short sentence."},
                      {"role": "user", "content": f"The code word is {canary}. What is 17 * 23?"}])
        answer = (r.choices[0].message.content or "").strip()
        checks["short_prompt"] = {"ok": "391" in answer, "s": round(time.monotonic() - t, 1),
                                  "finish": r.choices[0].finish_reason, "answer": answer[:200],
                                  "usage": r.usage.model_dump() if r.usage else None, "timings": timings_of(r)}
        step(f"short prompt: {answer[:80]!r}")

        t = time.monotonic()
        msgs = [{"role": "user", "content": "What's the local time in Lisbon right now? Use the tool."}]
        r = client.chat.completions.create(model=gc.SERVED_MODEL, max_tokens=1024, tools=TOOLS,
                                           tool_choice="auto", messages=msgs, **ROUTER_PARAMS)
        m = r.choices[0].message
        calls = m.tool_calls or []
        call_ok = len(calls) == 1 and calls[0].function.name == "get_local_time"
        try:
            call_args = json.loads(calls[0].function.arguments) if calls else {}
        except ValueError:
            call_args, call_ok = {}, False
        call_ok = call_ok and "lisbon" in str(call_args.get("city", "")).lower()
        tool_check = {"ok": call_ok, "s": round(time.monotonic() - t, 1), "finish": r.choices[0].finish_reason,
                      "n_calls": len(calls), "name": calls[0].function.name if calls else None, "args": call_args}
        if calls:
            msgs += [{"role": "assistant", "content": m.content or "",
                      "tool_calls": [c.model_dump() for c in calls[:1]]},
                     {"role": "tool", "tool_call_id": calls[0].id, "content": json.dumps({"time": "14:05", "tz": "WET"})}]
            r2 = client.chat.completions.create(model=gc.SERVED_MODEL, max_tokens=1024, tools=TOOLS,
                                                tool_choice="auto", messages=msgs, **ROUTER_PARAMS)
            final = (r2.choices[0].message.content or "").strip()
            tool_check["followup_ok"] = "14:05" in final or "2:05" in final
            tool_check["followup"] = final[:200]
        checks["tool_call"] = tool_check
        step(f"tool call: {tool_check}")

        t = time.monotonic()
        r = client.chat.completions.create(
            model=gc.SERVED_MODEL, max_tokens=1500, **ROUTER_PARAMS,
            messages=[{"role": "user", "content": "Write about 200 words on why lanterns were carried at sea."}])
        checks["decode"] = {"ok": bool((r.choices[0].message.content or "").strip()), "s": round(time.monotonic() - t, 1),
                            "finish": r.choices[0].finish_reason, "timings": timings_of(r),
                            "usage": r.usage.model_dump() if r.usage else None}
        step(f"decode: {checks['decode']['timings']}")

        logs = modal_cli("app", "logs", gc.APP_NAME, "--tail", "1000", timeout=120)
        checks["no_content_in_logs"] = {"ok": logs.returncode == 0 and canary not in logs.stdout + logs.stderr,
                                        "lines_checked": len(logs.stdout.splitlines())}
        ready = [ln.strip() for ln in logs.stdout.splitlines() if "lantern-glimmer ready" in ln]
        report["container_ready_log"] = ready[-1] if ready else None
    except Exception as e:   # still stop the app and write the report
        report["error"] = f"{type(e).__name__}: {e}"[:500]
        checks.setdefault("completed", {"ok": False})
    finally:
        if not args.keep:
            stop = modal_cli("app", "stop", "-y", gc.APP_NAME, timeout=300)
            checks["stopped"] = {"ok": stop.returncode == 0}
    gpu_s = time.monotonic() - gpu_t0 + (0 if not args.keep else cfg.idle_seconds)
    report["gpu_seconds"] = round(gpu_s, 1)
    report["estimated_cost_usd"] = round(gc.cost_usd(gpu_s, cfg), 4)
    report["all_ok"] = all(c.get("ok") for c in checks.values())

    out = json.dumps(report, indent=2)
    print(out)
    if args.report:
        Path(args.report).write_text(out + "\n")
    return 0 if report["all_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
