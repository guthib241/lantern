"""`python -m lantern --doctor`: everything Lantern needs on this computer, checked, with the exact fix.

Read-only by design: it never creates keys or files, never calls Meta's API (that's
scripts/smoke.py), and only probes the private-lane server when it runs on this machine.
Secrets are reported as present or missing, never shown.

Each check says what it is needed for:
  rehearsal  python -m lantern --demo-run / --serve --demo (no key, no accounts)
  demo       the real 3-minute demo (key, private lane, WhatsApp, email, desktop, face)
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from . import models
from .config import Config

REPO = Path(__file__).resolve().parent.parent
OK, WARN, FAIL, OFF = "ok", "warn", "fail", "off"
MARK = {OK: "✓", WARN: "!", FAIL: "✗", OFF: "○"}
PACKAGES = {"openai": "openai", "fastapi": "fastapi", "uvicorn": "uvicorn", "websockets": "websockets",
            "dotenv": "python-dotenv", "playwright": "playwright", "mcp": "mcp", "cryptography": "cryptography",
            "keyring": "keyring", "meta_sam_parser": "meta_sam_parser"}
WHATSAPP_VARS = ("WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_APP_SECRET", "WHATSAPP_VERIFY_TOKEN",
                 "LANTERN_WHATSAPP_OWNER")


@dataclass
class Check:
    id: str
    title: str
    status: str
    detail: str
    fix: str = ""
    needed_for: list[str] = field(default_factory=lambda: ["demo"])


@dataclass
class Env:
    """What the doctor looks at; tests swap in fakes."""
    vars: dict
    which: Callable[[str], str | None] = shutil.which
    has_module: Callable[[str], bool] = lambda name: importlib.util.find_spec(name) is not None
    probe: Callable[[str], bool] | None = None          # GET url -> reachable?
    chromium: Callable[[str], str | None] | None = None  # setting -> executable path or None
    run: Callable[[list[str]], str] | None = None       # argv -> stdout ("" on failure)


def _probe(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            return 200 <= r.status < 500
    except Exception:
        return False


def _run(argv: list[str]) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
                              cwd=REPO).stdout
    except Exception:
        return ""


def _chromium(setting: str) -> str | None:
    from .forms.driver import chromium_executable
    exe = chromium_executable(setting)
    if exe:
        return exe
    try:  # Playwright's own download (python -m playwright install chromium)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            path = p.chromium.executable_path
        return path if path and Path(path).exists() else None
    except Exception:
        return None


def run_checks(cfg: Config, env: Env | None = None) -> list[Check]:
    env = env or Env(dict(os.environ))
    v = env.vars
    probe, chromium, run = env.probe or _probe, env.chromium or _chromium, env.run or _run
    out: list[Check] = []
    add = out.append

    # -- basics ------------------------------------------------------------------------------
    py = sys.version_info
    add(Check("python", "Python 3.11+", OK if py >= (3, 11) else FAIL, f"{py.major}.{py.minor}.{py.micro}",
              "Install Python 3.11 or newer.", ["rehearsal", "demo"]))
    missing = [pip for mod, pip in PACKAGES.items() if not env.has_module(mod)]
    add(Check("packages", "Python packages", FAIL if missing else OK,
              "missing: " + ", ".join(missing) if missing else "all installed",
              "pip install -r requirements.txt", ["rehearsal", "demo"]))
    exe = chromium(cfg.chromium_path)
    add(Check("chromium", "Browser for forms", OK if exe else FAIL, exe or "no Chromium found",
              "python -m playwright install chromium   (or set LANTERN_CHROMIUM to a Chromium/Chrome binary)",
              ["rehearsal", "demo"]))

    # -- the model key -------------------------------------------------------------------------
    key = v.get(models.API_KEY_ENV, "")
    if not key:
        add(Check("key", "Meta Model API key", FAIL, f"{models.API_KEY_ENV} is not set",
                  f"Add {models.API_KEY_ENV}=<your key from dev.meta.ai> to .env, then run python scripts/smoke.py"))
    elif key != key.strip() or len(key) < 20:
        add(Check("key", "Meta Model API key", WARN, "set, but it looks cut short or has spaces around it",
                  f"Re-copy the key into .env as {models.API_KEY_ENV}=... (no quotes, no spaces)"))
    else:
        add(Check("key", "Meta Model API key", OK, f"set ({len(key)} characters; not shown)",
                  "Run python scripts/smoke.py once to prove it works"))
    tracked = run(["git", "ls-files", "--error-unmatch", ".env"]).strip()
    add(Check("env-file", ".env stays out of git", FAIL if tracked else OK,
              ".env is tracked by git!" if tracked else "not tracked",
              "git rm --cached .env && git commit -m 'stop tracking .env' (then rotate the key)", ["rehearsal", "demo"]))
    from .budget import ENV as BUDGET_ENV, Limits
    lim = Limits.from_env(v)
    bad = lim.problems(v)
    limits = [f"${x:g} {what}" if x is not None else f"no {what} limit"
              for x, what in ((lim.total, "total"), (lim.daily, "daily"), (lim.task, "per task"))]
    add(Check("budget", "Spending limits", WARN if bad or lim.total is None else OK,
              "; ".join(bad) + " (using the default)" if bad else ", ".join(limits),
              f"Use plain dollar amounts, e.g. {BUDGET_ENV['total']}=150" if bad else
              f"Set {BUDGET_ENV['total']}=150 so a runaway task can't spend the whole credit" if lim.total is None else ""))
    fm = v.get("LANTERN_FORMS_MODEL", cfg.forms_model)
    add(Check("forms-model", "Computer-use model", OK if fm == models.WONDER else WARN, fm,
              "" if fm == models.WONDER else f"Fine for testing; set LANTERN_FORMS_MODEL={models.WONDER} for the demo"))
    proto = v.get("LANTERN_CUA_PROTOCOL", "") or "auto"
    shown = (f"auto: {models.cua_protocol(fm)} for {fm} (measured against the live API)" if proto == "auto"
             else f"protocol {proto}, coordinates {v.get('LANTERN_CUA_COORDS', 'default')}")
    add(Check("cua", "Computer-use protocol", OK, shown,
              "" if proto == "auto" else "Remove LANTERN_CUA_PROTOCOL unless smoke.py cua told you to set it"))

    # -- private lane --------------------------------------------------------------------------
    url = v.get("LANTERN_GLIMMER_URL", "")
    if not url:
        add(Check("glimmer", "Private lane (Muse Glimmer)", WARN,
                  "not set: questions about private files are refused until it is",
                  "Serve Muse Glimmer with llama.cpp (llama-server ... --port 8080; see docs/GLIMMER.md), then set "
                  "LANTERN_GLIMMER_URL=http://127.0.0.1:8080/v1"))
    else:
        from .router import lane_for_url
        if lane_for_url(url) == "local":
            up = probe(url.rstrip("/") + "/models")
            add(Check("glimmer", "Private lane (Muse Glimmer)", OK if up else FAIL,
                      f"{url} {'answers' if up else 'does not answer'}",
                      "" if up else "Start the local Glimmer server, or fix LANTERN_GLIMMER_URL"))
        else:
            add(Check("glimmer", "Private lane (Muse Glimmer)", OK, f"self-hosted at {url} (not probed from here)"))

    # -- voice, phone, email ---------------------------------------------------------------------
    ff = env.which("ffmpeg")
    add(Check("ffmpeg", "ffmpeg (voice notes)", OK if ff else FAIL, ff or "not on PATH",
              "Install ffmpeg (winget install ffmpeg / brew install ffmpeg / apt install ffmpeg)"))
    channels = [c.strip().lower() for c in (v.get("LANTERN_CHANNELS") or v.get("LANTERN_CHANNEL") or "fake").split(",")]
    if "whatsapp" in channels:
        gone = [k for k in WHATSAPP_VARS if not v.get(k)]
        owner = v.get("LANTERN_WHATSAPP_OWNER", "")
        bad_owner = owner and not re.fullmatch(r"\d{8,15}", owner)
        add(Check("whatsapp", "WhatsApp", FAIL if gone or bad_owner else OK,
                  ("missing: " + ", ".join(gone)) if gone else
                  "LANTERN_WHATSAPP_OWNER must be digits only, with country code" if bad_owner else "configured",
                  "See README → Your phone: WhatsApp" if gone or bad_owner else ""))
        cf = env.which("cloudflared")
        add(Check("tunnel", "Tunnel for the WhatsApp webhook", OK if cf else WARN, cf or "cloudflared not found",
                  "" if cf else "Install cloudflared, then: cloudflared tunnel --url http://127.0.0.1:8765"))
    else:
        add(Check("whatsapp", "WhatsApp", WARN, "off: the phone is simulated (fine for rehearsal)",
                  "Set LANTERN_CHANNELS=whatsapp and the WhatsApp variables (README → Your phone)"))
    if "telegram" in channels:
        gone = [k for k in ("TELEGRAM_BOT_TOKEN", "LANTERN_TELEGRAM_OWNER") if not v.get(k)]
        add(Check("telegram", "Telegram", FAIL if gone else OK, "missing: " + ", ".join(gone) if gone else "configured",
                  "Create a bot with @BotFather and set both variables" if gone else "", []))
    mode = v.get("LANTERN_EMAIL_MODE", "fake")
    if mode == "smtp":
        gone = [k for k in ("LANTERN_SMTP_USER", "LANTERN_SMTP_PASSWORD") if not v.get(k)]
        add(Check("email", "Email (SMTP + IMAP)", FAIL if gone else OK,
                  "missing: " + ", ".join(gone) if gone else f"as {v.get('LANTERN_SMTP_USER')}",
                  "Gmail: turn on 2-step verification, create an app password, put both in .env" if gone else ""))
    else:
        add(Check("email", "Email (SMTP + IMAP)", WARN, "off: emails go to a local outbox folder",
                  "Set LANTERN_EMAIL_MODE=smtp, LANTERN_SMTP_USER, LANTERN_SMTP_PASSWORD (Gmail app password)"))

    # -- privacy setup -------------------------------------------------------------------------
    from .policy import Policy
    try:
        policy = Policy.load(cfg.zones_file, cfg.roots)
        zones = policy.describe()
        missing_roots = [r for r in cfg.roots if not Path(r).expanduser().exists()]
        private = [z for z in zones if z["zone"] == "private"]
        status = FAIL if missing_roots else OK if private else WARN
        detail = (f"missing folders: {', '.join(missing_roots)}" if missing_roots else
                  f"{len(zones)} zones, {len(private)} private" if private else "no private folder yet")
        fix = ("Create the folders or fix LANTERN_ROOTS" if missing_roots else "" if private else
               "python -m lantern --set-zone ~/Private private   (so the demo's private question stays local)")
        add(Check("zones", "Folders and privacy zones", status, detail, fix, ["demo"]))
    except Exception as e:
        add(Check("zones", "Folders and privacy zones", FAIL, f"zones.json can't be read: {e}",
                  f"Fix or delete {cfg.zones_file}"))
    if not cfg.encrypt:
        add(Check("encryption", "Memory encrypted at rest", WARN, "off (LANTERN_ENCRYPT=0)", "Unset LANTERN_ENCRYPT"))
    else:
        store = v.get("LANTERN_KEY_STORE", "auto")
        backend = _keyring_backend() if store != "file" else "key file (LANTERN_KEY_STORE=file)"
        add(Check("encryption", "Memory encrypted at rest", OK, f"on; key in {backend}"))

    # -- desktop ---------------------------------------------------------------------------------
    node = env.which("node")
    electron = (REPO / "desktop" / "node_modules" / "electron").exists()
    add(Check("desktop", "Desktop app", OK if node and electron else FAIL,
              "ready" if node and electron else "Node.js not found" if not node else "Electron not installed",
              "" if node and electron else "Install Node.js 18+, then: cd desktop && npm install"))
    try:
        servers = json.loads(Path(cfg.mcp_config).expanduser().read_text(encoding="utf-8")).get("servers", {})
    except (OSError, ValueError):
        servers = {}
    if "freecad" in servers:
        from .integrations.freecad.backend import find_freecadcmd
        cad = find_freecadcmd({**v, **(servers["freecad"].get("env") or {})})
        add(Check("freecad", "FreeCAD (CAD tools)", OK if cad else FAIL, cad or "FreeCAD not found",
                  "" if cad else "Install FreeCAD 1.0+ or set LANTERN_FREECAD_CMD in mcp.json's freecad env"))
    face = _avatar_expressions()
    add(Check("avatar", "Lantern's face (made with Muse)", OK if face else WARN,
              f"{face} expressions" if face else "none yet: the avatar is a faceless glow",
              "" if face else "python scripts/make_avatar.py   (or import MetaMuse art: python scripts/import_avatar.py <folder>)"))
    return out


def _keyring_backend() -> str:
    try:
        import keyring
        name = type(keyring.get_keyring()).__name__
        return "a key file (no usable OS keychain)" if "fail" in name.lower() else f"the OS keychain ({name})"
    except Exception:
        return "a key file (keyring not installed)"


def _avatar_expressions() -> int:
    try:
        data = json.loads((REPO / "desktop" / "assets" / "avatar" / "manifest.json").read_text(encoding="utf-8"))
        return len(data.get("expressions") or {})
    except (OSError, ValueError):
        return 0


def readiness(checks: list[Check]) -> dict:
    """For each goal: the checks that block it (fail) and the ones still to do (warn)."""
    return {goal: {"blocked_by": [c.id for c in checks if goal in c.needed_for and c.status == FAIL],
                   "to_do": [c.id for c in checks if goal in c.needed_for and c.status == WARN]}
            for goal in ("rehearsal", "demo")}


def report(checks: list[Check]) -> str:
    lines = []
    for c in checks:
        lines.append(f" {MARK[c.status]} {c.title:<34} {c.detail}")
        if c.fix and c.status in (FAIL, WARN):
            lines.append(f"   → {c.fix}")
    ready = readiness(checks)
    lines.append("")
    for goal, label in (("rehearsal", "Rehearsal (--demo-run)"), ("demo", "Real demo")):
        r = ready[goal]
        line = f" {label}: " + ("ready ✓" if not r["blocked_by"] else f"blocked by {', '.join(r['blocked_by'])}")
        if r["to_do"]:
            line += f" · still to do: {', '.join(r['to_do'])}"
        lines.append(line)
    return "\n".join(lines)


def as_json(checks: list[Check]) -> dict:
    return {"checks": [asdict(c) for c in checks], "ready": readiness(checks)}
