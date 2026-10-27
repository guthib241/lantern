"""The Routine Contract (S4): what a compiled routine does, as data, before any code exists.

  {"name": "city-permit-application", "title": "...", "kind": "web" | "files",
   "examples": ["fill the city permit application"],
   "params": [{"name": "base_url", "type": "url", "default": "http://127.0.0.1:8770"},
              {"name": "permit_type", "type": "enum", "values": ["Home renovation", ...], "default": "..."}],
   "steps": [{"id": "s1", "say": "Open the personal details page", "action": "browser.open",
              "args": {"url": "{{base_url}}/apply/personal"},
              "post": [{"check": "browser.url_contains", "args": {"text": "/apply/personal"}}]}, ...],
   "success": [{"check": "browser.text_matches", "args": {"pattern": "CPP-\\\\d+-\\\\d+"}}],
   "source": {"task_id": 12, "cost_usd": 0.05, "seconds": 80, "model_calls": 14}}

Rules enforced here, in code:
  - Only the actions and checks below exist. A step's action and target come from what the recorded run
    actually did (`same_plan`): a model may name, describe and parameterize steps, never add or retarget one.
  - Private values are never literals: anything that came from the profile vault becomes {{vault.<key>}},
    and any other value that looks private (scrubber.py) becomes a `secret` param with no stored default.
  - Every {{placeholder}} refers to a declared param or to the vault.
"""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

from ..scrubber import DEFAULT as SCRUBBER

# action -> (required args, optional args, risk). Risk: green (read), yellow (reversible, local), red (send/submit/move).
ACTIONS: dict[str, tuple[tuple[str, ...], tuple[str, ...], str]] = {
    "browser.open": (("url",), (), "green"),
    "browser.fill": (("field", "value"), ("name", "html_id", "type"), "yellow"),
    "browser.upload": (("field", "path"), ("name", "html_id"), "yellow"),
    "browser.submit": (("button",), (), "red"),
    "files.move": (("source", "destination"), (), "red"),
}
CHECKS: dict[str, tuple[str, ...]] = {
    "browser.url_contains": ("text",),
    "browser.text_contains": ("text",),
    "browser.text_matches": ("pattern",),
    "browser.field_equals": ("field", "value"),
    "files.exists": ("path",),
    "files.missing": ("path",),
}
PARAM_TYPES = ("string", "enum", "url", "path", "secret")
NAME = re.compile(r"^[a-z][a-z0-9-]{1,59}$")
PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z_][\w.]*)\s*\}\}")


class ContractError(ValueError):
    """The contract breaks a rule; the message says which."""


def risk(action: str) -> str:
    return ACTIONS[action][2]


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:50].strip("-")
    return s if NAME.match(s or "") else ("routine-" + s)[:50].strip("-")


def placeholders(value: Any) -> list[str]:
    if isinstance(value, str):
        return PLACEHOLDER.findall(value)
    if isinstance(value, dict):
        return [p for v in value.values() for p in placeholders(v)]
    if isinstance(value, list):
        return [p for v in value for p in placeholders(v)]
    return []


def fill_in(value: Any, params: dict, vault: dict) -> Any:
    """Resolve {{param}} and {{vault.key}}. A missing value raises KeyError with the placeholder."""
    if isinstance(value, dict):
        return {k: fill_in(v, params, vault) for k, v in value.items()}
    if isinstance(value, list):
        return [fill_in(v, params, vault) for v in value]
    if not isinstance(value, str):
        return value

    def one(m: re.Match) -> str:
        key = m.group(1)
        if key.startswith("vault."):
            k = key[len("vault."):]
            for cand in (k, k.replace("_", " ")):
                if cand in vault and str(vault[cand]).strip():
                    return str(vault[cand])
            raise KeyError(key)
        if key not in params or params[key] in (None, ""):
            raise KeyError(key)
        return str(params[key])
    return PLACEHOLDER.sub(one, value)


def looks_private(value: str) -> bool:
    return bool(SCRUBBER.scrub(str(value)).redactions)


# ---- validation ------------------------------------------------------------------------
def validate(c: dict) -> list[str]:
    """Problems with a contract (empty list = fine)."""
    out: list[str] = []
    if not isinstance(c, dict):
        return ["the contract must be an object"]
    if not NAME.match(str(c.get("name", ""))):
        out.append("name must be lowercase letters, digits and dashes (2-60)")
    if c.get("kind") not in ("web", "files"):
        out.append("kind must be 'web' or 'files'")
    params = c.get("params") or []
    names = set()
    for p in params:
        if not isinstance(p, dict) or not re.match(r"^[a-z_][a-z0-9_]{0,40}$", str(p.get("name", ""))):
            out.append(f"bad param {p!r}")
            continue
        if p.get("type") not in PARAM_TYPES:
            out.append(f"param {p['name']}: type must be one of {PARAM_TYPES}")
        if p.get("type") == "enum" and not p.get("values"):
            out.append(f"param {p['name']}: an enum needs values")
        if p.get("type") == "secret" and p.get("default") not in (None, ""):
            out.append(f"param {p['name']}: a secret never has a stored default")
        if p.get("default") not in (None, "") and p.get("type") != "secret" and looks_private(str(p["default"])):
            out.append(f"param {p['name']}: its default looks private; use a vault placeholder")
        names.add(p["name"])
    steps = c.get("steps") or []
    if not steps:
        out.append("a routine needs at least one step")
    ids = set()
    for s in steps:
        sid = s.get("id") if isinstance(s, dict) else None
        if not sid or sid in ids:
            out.append(f"step ids must be present and unique ({sid!r})")
        ids.add(sid)
        act = s.get("action") if isinstance(s, dict) else None
        if act not in ACTIONS:
            out.append(f"step {sid}: unknown action {act!r}")
            continue
        req, opt, _ = ACTIONS[act]
        args = s.get("args") or {}
        missing = [a for a in req if a not in args]
        extra = [a for a in args if a not in req + opt]
        if missing or extra:
            out.append(f"step {sid}: {act} needs {list(req)}" + (f", got extra {extra}" if extra else ""))
        out += _checks(f"step {sid}", s.get("post") or [])
        out += _literals(f"step {sid}", args)
    out += _checks("success", c.get("success") or [])
    for key in placeholders([s.get("args") for s in steps] + [s.get("post") for s in steps] + [c.get("success")]):
        if not key.startswith("vault.") and key not in names:
            out.append(f"placeholder {{{{{key}}}}} is not a declared param")
    return out


def _checks(where: str, checks: list) -> list[str]:
    out = []
    for ch in checks:
        if not isinstance(ch, dict) or ch.get("check") not in CHECKS:
            out.append(f"{where}: unknown check {ch!r}")
            continue
        args = ch.get("args") or {}
        if sorted(args) != sorted(CHECKS[ch["check"]]):
            out.append(f"{where}: {ch['check']} needs {list(CHECKS[ch['check']])}")
        if ch["check"] == "browser.text_matches":
            try:
                re.compile(str(args.get("pattern", "")))
            except re.error as e:
                out.append(f"{where}: bad pattern ({e})")
    return out


def _literals(where: str, args: dict) -> list[str]:
    out = []
    for k, v in args.items():
        if k in ("value", "path") and isinstance(v, str) and not placeholders(v) and looks_private(v):
            out.append(f"{where}: the {k} looks private; it must be a vault placeholder or a secret param")
    return out


def plan_of(c: dict) -> list[tuple[str, str]]:
    """What the routine does, ignoring wording and values: (action, target) per step, in order."""
    out = []
    for s in c.get("steps") or []:
        a = s.get("args") or {}
        target = a.get("url", "") if s.get("action") == "browser.open" else (
            a.get("field") or a.get("button") or a.get("source") or "")
        if s.get("action") == "browser.open":
            target = urlparse(str(target).replace("{{base_url}}", "http://base")).path
        out.append((str(s.get("action")), str(target)))
    return out


def same_plan(proposed: dict, recorded: dict) -> list[str]:
    """A model's contract may rename, describe, parameterize and add checks, but must do exactly what the
    recorded run did, step for step, with the same defaults."""
    a, b = plan_of(proposed), plan_of(recorded)
    if a != b:
        return [f"the steps differ from the recorded run ({len(a)} vs {len(b)}; first difference at "
                f"{next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))})"]
    try:
        defaults = {p["name"]: p.get("default") for p in proposed.get("params") or []}
        rec_defaults = {p["name"]: p.get("default") for p in recorded.get("params") or []}
        for s, r in zip(proposed["steps"], recorded["steps"]):
            for k in ("value", "path", "url", "source", "destination"):
                if k in r.get("args", {}):
                    mine = _resolve_defaults(s["args"].get(k), defaults)
                    theirs = _resolve_defaults(r["args"][k], rec_defaults)
                    if mine != theirs:
                        return [f"step {s.get('id')}: {k} differs from what the recorded run used"]
    except (KeyError, TypeError) as e:
        return [f"couldn't compare with the recorded run: {e}"]
    return []


def _resolve_defaults(value: Any, defaults: dict) -> Any:
    if not isinstance(value, str):
        return value
    return PLACEHOLDER.sub(lambda m: m.group(0) if m.group(1).startswith("vault.") else
                          str(defaults.get(m.group(1), m.group(0))), value)


# ---- the recorded run -> a skeleton contract (no model) -------------------------------------
def _vault_key(value: str, vault: dict) -> str | None:
    v = str(value).strip()
    for k, val in vault.items():
        if str(val).strip() and str(val).strip() == v:
            return k.replace(" ", "_")
    return None


def _value(value: str, vault: dict, params: list, hint: str) -> str:
    """A recorded value as the contract stores it: a vault placeholder, a secret param, or the literal."""
    key = _vault_key(value, vault)
    if key:
        return "{{vault." + key + "}}"
    if looks_private(value):
        name = re.sub(r"[^a-z0-9]+", "_", hint.lower()).strip("_")[:30] or "secret"
        params.append({"name": name, "type": "secret", "say": f"{hint} (asked each time, never stored)"})
        return "{{" + name + "}}"
    return value


def _pattern(evidence: str) -> str:
    """A regex for a confirmation like 'Reference CPP-2026-000001': same words, any digits."""
    first = next((line.strip() for line in evidence.splitlines() if re.search(r"\d", line)), evidence.strip())[:80]
    m = re.search(r"[A-Z]{2,}[-/]?\d[\w-]*", first)
    token = m.group(0) if m else first
    # Segments with a digit vary per confirmation (CPP-2026-4F9A1C): any letters or digits there.
    parts = re.split(r"([-/ ])", token)
    return "".join(re.escape(p) if not re.search(r"\d", p) else "[A-Za-z0-9]+" for p in parts)


def skeleton_from_traces(goal: str, traces: list[dict], vault: dict, source: dict) -> dict:
    """The recorded fill_form traces as a contract, exactly as they happened."""
    if not traces:
        raise ContractError("this task didn't fill any form, so there is nothing to compile")
    for t in traces:
        if t.get("visual"):
            raise ContractError("a page needed computer use for " + ", ".join(t["visual"][:3])
                                + "; routines can only replay DOM steps for now")
        if t.get("left_for_you"):
            raise ContractError("the form had fields only the user may fill (" + ", ".join(t["left_for_you"][:3])
                                + "); a routine can't do those")
        if not t.get("submitted"):
            raise ContractError("a page in the recorded run was not submitted")
    if not traces[-1].get("verified"):
        raise ContractError("the recorded run's last page wasn't verified, so there is no proof it worked")
    first = urlparse(traces[0]["url"])
    base = f"{first.scheme}://{first.netloc}"
    params: list[dict] = [{"name": "base_url", "type": "url", "default": base, "say": "where the form lives"}]
    steps: list[dict] = []
    here = ""
    for t in traces:
        url = urlparse(t["url"])
        if f"{url.scheme}://{url.netloc}" != base:
            raise ContractError("the recorded run used more than one site")
        if url.path != here:
            steps.append({"id": f"s{len(steps) + 1}", "say": f"Open {url.path or '/'}", "action": "browser.open",
                          "args": {"url": "{{base_url}}" + url.path + (f"?{url.query}" if url.query else "")},
                          "post": [{"check": "browser.url_contains", "args": {"text": url.path}}]})
        for f in t.get("fields") or []:
            label = f.get("label") or f.get("name") or f.get("html_id")
            hints = {k: f[k] for k in ("name", "html_id") if f.get(k)}
            if f.get("type") == "file":
                steps.append({"id": f"s{len(steps) + 1}", "say": f"Attach {base_name(f['value'])} as {label}",
                              "action": "browser.upload", "args": {"field": label, "path": f["value"], **hints},
                              "post": []})
                continue
            options = [o if isinstance(o, dict) else {"value": o, "text": o} for o in f.get("options") or []]
            keys = [o["value"] if o.get("value") and not str(o["value"]).isdigit() else o["text"] for o in options]
            if f.get("type") in ("select", "radio") and f["value"] in keys:
                name = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:30] or f"choice{len(params)}"
                params.append({"name": name, "type": "enum", "values": keys, "default": f["value"], "say": label,
                               "labels": {k: o["text"] for k, o in zip(keys, options)}})
                value = "{{" + name + "}}"
            else:
                value = _value(f["value"], vault, params, label)
            post = [] if f.get("type") in ("checkbox", "radio", "select") else [
                {"check": "browser.field_equals", "args": {"field": label, "value": value}}]
            steps.append({"id": f"s{len(steps) + 1}", "say": f"Fill {label}", "action": "browser.fill",
                          "args": {"field": label, "value": value, "type": f.get("type", "text"), **hints},
                          "post": post})
        nxt = urlparse(t.get("final_url") or "")
        post = [{"check": "browser.url_contains", "args": {"text": nxt.path}}] if nxt.path and nxt.path != url.path else []
        steps.append({"id": f"s{len(steps) + 1}", "say": f"Press {t['submit']}", "action": "browser.submit",
                      "args": {"button": t["submit"]}, "post": post})
        here = nxt.path
    success = []
    last = traces[-1]
    if last.get("evidence"):
        success.append({"check": "browser.text_matches", "args": {"pattern": _pattern(last["evidence"])}})
    if urlparse(last.get("final_url") or "").path:
        success.append({"check": "browser.url_contains", "args": {"text": urlparse(last["final_url"]).path}})
    return {"name": slug(goal_words(goal)), "title": goal_words(goal)[:80], "kind": "web",
            "examples": [goal_words(goal)], "params": params, "steps": steps, "success": success, "source": source}


def base_name(path: str) -> str:
    return re.split(r"[\\/]", str(path))[-1]


def goal_words(goal: str) -> str:
    """The goal as the user said it, without a channel tag or a resume note."""
    g = re.sub(r"^\[(whatsapp|telegram|fake|mcp)\]\s*", "", goal.strip(), flags=re.I)
    return g.split("\n\n[Resuming task #")[0].strip()


def dumps(c: dict) -> str:
    return json.dumps(c, indent=1, ensure_ascii=False)
