"""Rehearsal mode: the whole 3-minute demo story, offline and free.

  python -m lantern --demo-run          plays the story in the terminal and prints a transcript
  python -m lantern --serve --demo      serves it to the desktop app and the simulated phone

Everything except the model runs for real, on a fresh copy of demo/home/: privacy zones, the
scrubber and audit log, approvals bound to payload hashes (tapped on the simulated phone),
the browser filling demo/forms/, the email outbox, the environment map, the widget checker,
and every verification. Only the model is scripted: DemoModel answers these goals the way Muse
Spark / Muse Wonder would and reports zero tokens. Nothing leaves the laptop, and the audit log's
destination says so ("rehearsal://nothing-sent").
"""
from __future__ import annotations

import itertools
import json
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any, Callable, Iterator

from . import config as config_mod
from . import models
from .audit import AuditLog
from .crypto import load_cipher
from .email_tool import EmailService, EmailSettings
from .forms import Vault
from .forms.filler import DEMO_FORMS, REPO_DIR
from .memory import Memory
from .policy import Policy
from .widgets import REPO_WIDGETS, WIDGET_RULES

DEMO_HOME = REPO_DIR / "demo" / "home"
DEMO_PROFILE = REPO_DIR / "demo" / "profile.example.json"
DESTINATION = "rehearsal://nothing-sent"
ORGANISER = "organiser@openbuild.example"


@dataclass(frozen=True)
class Beat:
    """One moment of the story: what the user says, and where they say it."""
    goal: str
    via: str = "desktop"   # desktop | voice (a voice note on the simulated phone)


STORY = [
    Beat("Fill the hackathon reimbursement form for my taxi and email it to the organiser.", via="voice"),
    Beat("What base salary does the offer letter in my Private folder say?"),
    Beat("Anything I forgot in my project notes?"),
    Beat("Which projects use torch?"),
    Beat("Make me a water timer."),
]

HELP = ("This is rehearsal mode, so I only know the demo script. Try: fill the hackathon reimbursement "
        "form and email it to the organiser · what does my offer letter say? · anything I forgot in my "
        "project notes? · which projects use torch? · make me a water timer · remind me in 20 minutes to "
        "record the video · tidy my Downloads.")
EXTRAS = [Beat("Remind me in 20 minutes to record the demo video."),   # served by --serve --demo, not --demo-run
          Beat("Tidy my Downloads folder.")]


class DemoError(RuntimeError):
    pass


# ---- the scripted model --------------------------------------------------------------------
def _usage():
    return NS(input_tokens=0, input_tokens_details=NS(cached_tokens=0), output_tokens=0,
              output_tokens_details=NS(reasoning_tokens=0), total_tokens=0)


_ids = itertools.count(1)


def _call(tool: str, /, **args) -> NS:
    return NS(type="function_call", name=tool, call_id=f"call_{tool}_{next(_ids)}",
              arguments=json.dumps(args, ensure_ascii=False))


class _Say:
    """A step that answers in words instead of calling a tool."""

    def __init__(self, text: str):
        self.text = text


Story = Iterator[Any]   # yields a function call (NS) or _Say; receives the tool results (list of dicts)


class DemoModel:
    """Stands in for the Responses API during rehearsal. Same `.responses.create` surface.

    It recognises who is asking by the request's shape: the agent (has the `finish` tool),
    the form planner (`set_values`), the computer-use loop (the `computer` tool) or the
    widget writer (the widget rules). Agent conversations are generators keyed by response id,
    so `previous_response_id` chaining works exactly as with the real API."""

    def __init__(self, locate: Callable[[], dict] | None = None):
        self.responses = self
        self.chat = NS(completions=self)   # structured calls (lesson plan / lesson question) are scripted too
        self.locate = locate or measure_form
        self._spots: dict | None = None
        self._chains: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._n = 0
        self.requests: list[dict] = []

    def create(self, **kw) -> NS:
        with self._lock:
            self._n += 1
            rid = f"demo_resp_{self._n}"
            self.requests.append(kw)
        if kw.get("response_format"):   # a structured Chat Completions call (the lesson planner / board question)
            return self._chat(rid, kw)
        tools = kw.get("tools") or []
        names = {t.get("name") for t in tools if t.get("type") == "function"}
        if kw.get("model") == models.SAM:
            raise DemoError("the camera check-in isn't part of the offline rehearsal")
        if any(t.get("type") == "computer" for t in tools):
            return self._computer(rid, kw)
        if "set_values" in names:
            return self._reply(rid, [_call("set_values", values=plan_values(str(kw.get("input", ""))))])
        if str(kw.get("instructions", "")).startswith(WIDGET_RULES[:80]):
            html = (REPO_WIDGETS / "water-timer.html").read_text(encoding="utf-8")
            return self._reply(rid, [], f"Here's the widget.\n```html\n{html}\n```")
        if "finish" in names:
            return self._agent(rid, kw)
        raise DemoError("that request isn't part of the rehearsal script")

    def _chat(self, rid: str, kw: dict) -> NS:
        """The scripted structured answers: a canned factoring lesson, and a canned answer to a board
        question that highlights the last verified line it was given. Anything else isn't in the script."""
        name = kw["response_format"]["json_schema"]["name"]
        usage = NS(prompt_tokens=0, completion_tokens=0, prompt_tokens_details=NS(cached_tokens=0),
                   completion_tokens_details=NS(reasoning_tokens=0))
        if name == "lesson_plan":
            data: Any = canned_lesson()
        elif name == "lesson_ask":
            try:
                given = json.loads(kw["messages"][-1]["content"])
            except (ValueError, IndexError, KeyError):
                given = {}
            lines = given.get("verified_lines_so_far") or []
            last = lines[-1].get("id") if lines and isinstance(lines[-1], dict) else None
            data = {"related": True,
                    "spoken": ("Good question. Each line on the board follows from the one before it, and "
                               "SymPy checked it: we need two numbers that multiply to six and add to "
                               "minus five, and those are minus two and minus three."),
                    "actions": [{"do": "highlight", "target": last, "say": "This line is the key step."}] if last else []}
        else:
            raise DemoError("that request isn't part of the rehearsal script")
        return NS(id=rid, choices=[NS(message=NS(content=json.dumps(data)))], usage=usage)

    # -- plumbing --
    @staticmethod
    def _reply(rid: str, output: list, text: str = "") -> NS:
        return NS(id=rid, output=output, output_text=text, usage=_usage())

    def _step(self, rid: str, story: Story, results: list | None) -> NS:
        try:
            item = next(story) if results is None else story.send(results)
        except StopIteration:
            return self._reply(rid, [], "Done.")
        if isinstance(item, _Say):
            return self._reply(rid, [], item.text)
        with self._lock:
            self._chains[rid] = story   # continued by the request whose previous_response_id is rid
        return self._reply(rid, [item])

    def _agent(self, rid: str, kw: dict) -> NS:
        prev = kw.get("previous_response_id")
        if prev:
            with self._lock:
                story = self._chains.pop(prev, None)
            if story is None:
                return self._reply(rid, [], HELP)
            results = []
            for item in kw.get("input") or []:
                if isinstance(item, dict) and item.get("type") == "function_call_output":
                    try:
                        results.append(json.loads(item.get("output") or "{}"))
                    except json.JSONDecodeError:
                        results.append({"error": "unreadable tool output"})
            return self._step(rid, story, results)
        goal = str(kw.get("input", ""))
        return self._step(rid, pick_story(goal, str(kw.get("instructions", ""))), None)

    def _computer(self, rid: str, kw: dict) -> NS:
        """Pick 'Travel' in the custom category dropdown: click it open, click the option, report."""
        prev = kw.get("previous_response_id")
        with self._lock:
            step = self._chains.pop(prev, 0) if prev else 0
        if self._spots is None:
            try:
                self._spots = _in_thread(self.locate)  # Playwright's sync API can't nest in this thread
            except Exception as e:  # no browser to measure with: say so, the form check will catch it
                return self._reply(rid, [], f"I couldn't find the category dropdown ({type(e).__name__}).")
        targets = [self._spots.get("category"), self._spots.get("travel")]
        if step >= len(targets) or targets[step] is None:
            return self._reply(rid, [], "Picked Travel as the expense category.")
        x, y = targets[step]
        with self._lock:
            self._chains[rid] = step + 1
        action = {"type": "click", "button": "left", "x": x, "y": y}
        return self._reply(rid, [NS(type="computer_call", call_id=f"demo_cua_{rid}", actions=[action],
                                    pending_safety_checks=[], status="completed")])


def _in_thread(fn: Callable[[], Any], timeout: float = 60.0) -> Any:
    box: dict = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except Exception as e:
            box["error"] = e
    t = threading.Thread(target=run, name="lantern-demo-measure", daemon=True)
    t.start()
    t.join(timeout)
    if "error" in box:
        raise box["error"]
    if "value" not in box:
        raise DemoError("measuring the demo form timed out")
    return box["value"]


def measure_form(chromium: str = "") -> dict:
    """Where the category dropdown and its 'Travel' option sit in Lantern's 1280x800 browser,
    measured the same way the tests do (the rehearsal model has no eyes)."""
    from .forms.driver import BrowserDriver, chromium_executable

    def centre(box: dict) -> tuple[int, int]:
        return round(box["x"] + box["width"] / 2), round(box["y"] + box["height"] / 2)
    with tempfile.TemporaryDirectory(prefix="lantern-measure-") as tmp:
        with BrowserDriver(tmp, executable_path=chromium_executable(chromium)) as d:
            d.open((DEMO_FORMS / "reimbursement.html").as_uri())
            spots = {"category": centre(d.page.locator("#category").bounding_box())}
            d.page.click("#category")
            spots["travel"] = centre(d.page.locator('li[data-value="travel"]').bounding_box())
    return spots


def canned_lesson() -> dict:
    """The rehearsal's one lesson: solve x^2 - 5x + 6 = 0 by factoring (every step SymPy-checkable)."""
    return {"title": "Solve x² − 5x + 6 = 0", "subject": "algebra", "language": "en-IN",
            "summary": "Factor it, then use the zero-product rule.",
            "actions": [
                {"do": "write_text", "id": "title", "text": "Solve x² − 5x + 6 = 0", "x": 80, "y": 50,
                 "size": 46, "color": "yellow", "say": "Let's solve x squared minus five x plus six equals zero."},
                {"do": "write_math", "id": "eq0", "latex": "x^2 - 5x + 6 = 0", "x": 80, "y": 150,
                 "check": {"sympy": "Eq(x**2 - 5*x + 6, 0)", "relation": "given", "variable": "x"},
                 "say": "We need two numbers that multiply to six and add to minus five."},
                {"do": "write_math", "id": "eq1", "latex": "(x - 2)(x - 3) = 0", "x": 80, "y": 240,
                 "step": "factor",
                 "check": {"sympy": "Eq((x - 2)*(x - 3), 0)", "relation": "same_solutions", "of": "eq0",
                           "variable": "x"},
                 "say": "Minus two and minus three work, so it factors like this."},
                {"do": "highlight", "target": "eq1", "say": "A product is zero when one of its factors is zero."},
                {"do": "write_math", "id": "eq2", "latex": "x = 2 \\text{ or } x = 3", "x": 80, "y": 330,
                 "step": "solve each", "color": "green",
                 "check": {"sympy": "[Eq(x, 2), Eq(x, 3)]", "relation": "same_solutions", "of": "eq0"},
                 "say": "So x is two, or x is three."},
                {"do": "pause", "seconds": 1},
            ]}


def pick_story(goal: str, instructions: str = "") -> Story:
    g = goal.lower()
    if "remind" in g:
        return _reminder_story(goal)
    if "form" in g or "reimburse" in g:
        m = re.search(r"organi[sz]er[^\n]*?([\w.+-]+@[\w-]+(?:\.[\w-]+)+)", instructions, re.I)
        return _forms_story(m.group(1) if m else ORGANISER)
    if re.search(r"\b(tidy|clean up|organi[sz]e)\b", g):
        m = re.search(r"\b(downloads|documents)\b", g)
        return _tidy_story(m.group(1).capitalize() if m else "Downloads")
    if "offer" in g or "salary" in g or "private" in g:
        return _private_story()
    if "notes" in g or "forgot" in g:
        return _notes_story()
    if "which project" in g or "use torch" in g or "uses torch" in g:
        m = re.search(r"\buses? ([A-Za-z0-9_.-]+)", goal)
        return _projects_story(m.group(1).strip(".?") if m else "torch")
    if "water" in g or "timer" in g or "widget" in g:
        return _widget_story()
    return _say_story(HELP)


def _reminder_story(goal: str) -> Story:
    when = re.search(r"\b(in \d+ (?:minutes?|hours?|days?)|at \d{1,2}(?::\d{2})?\s*(?:am|pm)?)", goal, re.I)
    what = re.search(r"\bto (.+?)[.!?]?$", goal)
    [r] = yield _call("set_reminder", text=what.group(1) if what else goal, when=when.group(1) if when else "in 20 minutes")
    if r.get("verified"):
        yield _Say(f"Set for {r['due']}: “{r['text']}”. Your phone and the desktop will hear about it.")
    else:
        yield _Say(f"No reminder: {r.get('error') or r.get('reason', 'not set')}.")


TIDY = {"Images": (".jpg", ".jpeg", ".png", ".gif", ".heic", ".webp"),
        "Documents": (".pdf", ".docx", ".txt", ".md"), "Spreadsheets": (".csv", ".xlsx")}


def _tidy_story(folder: str) -> Story:
    [listing] = yield _call("list_dir", path=folder)
    if listing.get("error"):
        yield _Say(f"I can't look in {folder}: {listing['error']}")
        return
    moves = []
    for entry in listing.get("entries", []):
        name = entry["name"]
        group = next((g for g, exts in TIDY.items() if Path(name).suffix.lower() in exts), None)
        if group and not name.endswith("/"):
            moves.append({"source": f"{folder}/{name}", "destination": f"{folder}/{group}/"})
    if not moves:
        yield _Say(f"{folder} is already tidy. Suspiciously tidy.")
        return
    [r] = yield _call("organize_files", moves=moves)
    if r.get("verified"):
        where = ", ".join(f"{n} into {Path(f).name}" for f, n in r.get("folders", {}).items())
        yield _Say(f"Moved {r['moved']} files: {where}. Every move checked on disk. Say “undo” and I'll put them back.")
    else:
        yield _Say(f"Nothing moved: {r.get('error') or r.get('reason') or r.get('failed')}.")


def _say_story(text: str) -> Story:
    yield _Say(text)


def _forms_story(organiser: str) -> Story:
    [found] = yield _call("search_files", query="taxi receipt")
    receipt = next((h["path"] for h in found.get("results", [])), "")
    [form] = yield _call("fill_form", url="hackathon reimbursement",
                         goal=("Claim my taxi to the hackathon venue: INR 2400 on 25 Oct 2026, category Travel, "
                               f"purpose 'Taxi to the hackathon venue'. Receipt file: {receipt}"))
    if not form.get("verified"):
        why = form.get("error") or form.get("reason") or "the confirmation page never appeared"
        yield _call("finish", status="failed", summary=f"form not submitted: {why}")
        yield _Say(f"The form didn't go through: {why}. Nothing was emailed.")
        return
    ref = re.search(r"REF-[A-Z0-9-]+", json.dumps(form.get("confirmation", {}))) or None
    ref_text = ref.group(0) if ref else "the confirmation page"
    [mail] = yield _call("send_email", to=[organiser], subject=f"Travel reimbursement request ({ref_text})",
                         body=(f"Hi,\n\nI've submitted the hackathon reimbursement form for my taxi to the venue "
                               f"(INR 2,400, 25 Oct). Reference: {ref_text}. The receipt is attached.\n\n"
                               "Thanks!\n(sent by Lantern)"),
                         attachments=[receipt] if receipt else [])
    ok = bool(mail.get("verified"))
    yield _call("finish", status="done" if ok else "failed",
                summary=f"submitted the reimbursement form ({ref_text})" + ("; emailed the organiser" if ok else ""))
    if ok:
        yield _Say(f"Form submitted and checked on the confirmation page ({ref_text}), and the receipt is in "
                   f"the organiser's inbox. I left the payout card, CVV and PAN for you. You're welcome.")
    else:
        yield _Say(f"The form went in ({ref_text}), but the email didn't: {mail.get('reason', 'not sent')}.")


def _private_story() -> Story:
    [r] = yield _call("ask_private", path="Private/offer_letter.txt", question="What is the base salary?")
    if r.get("error"):
        yield _Say(f"I couldn't ask the private lane: {r['error']}")
        return
    yield _Say(f"{r.get('answer', '')}\nI asked {r.get('model', 'the local model')} on this computer. The letter "
               "itself never left; only that answer reached me. Check the audit view if you don't believe me.")


def _notes_story() -> Story:
    [r] = yield _call("read_file", path="Documents/project_notes.md")
    text = str(r.get("content", ""))
    todo = [line[6:].strip() for line in text.splitlines() if line.strip().startswith("- [ ]")]
    redacted = sorted(set(re.findall(r"\[REDACTED:([a-z_]+)\]", text)))
    lines = ["Still open: " + "; ".join(todo) + "." if todo else "Nothing open in your notes."]
    if redacted:
        lines.append(f"Also, your notes contain a secret ({', '.join(k.replace('_', ' ') for k in redacted)}). "
                     "It was redacted before this request left your laptop, so I never saw it. Rotate it anyway.")
    yield _Say(" ".join(lines))


def _projects_story(package: str) -> Story:
    [r] = yield _call("which_projects_use", package=package)
    hits = r.get("projects", [])
    if not hits:
        yield _Say(f"None of your {r.get('scanned_projects', 0)} projects use {package}.")
        return
    parts = []
    for h in hits:
        installed = ", ".join(sorted(set((h.get("installed") or {}).values())))
        parts.append(f"{h['project']} ({'installed ' + installed if installed else 'declared ' + (h.get('declared') or '?')})")
    yield _Say(f"{len(hits)} of your {r.get('scanned_projects', len(hits))} projects use {package}: "
               + ", ".join(parts) + ". That came from the local project map; the lookup itself needs no model call.")


def _widget_story() -> Story:
    [r] = yield _call("make_widget", name="water-timer",
                      description="A 45-minute water break timer with start/pause/reset and a cheeky notification. "
                                  "A plain timer: no health advice.")
    if r.get("saved"):
        yield _Say("Your water timer is in the Widgets tab. It's a timer, not a doctor. Sandboxed, no network, "
                   "no files.")
    else:
        yield _Say(f"No widget this time: {r.get('error') or r.get('reason', 'not saved')}.")


# ---- the form planner's half ---------------------------------------------------------------
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                      "nov", "dec"], 1)}


def _section(text: str, title: str) -> Any:
    m = re.search(rf"{title}:\n(.*?)(?:\n\n[A-Z][^\n]*:\n|\Z)", text, re.S)
    try:
        return json.loads(m.group(1)) if m else None
    except json.JSONDecodeError:
        return None


def plan_values(text: str) -> list[dict]:
    """What the planner would answer for the demo form, from the goal and the profile it was sent."""
    m = re.search(r"Goal:\n(.*?)\n\nForm fields:", text, re.S)
    goal = m.group(1) if m else ""
    fields = _section(text, "Form fields") or []
    profile = (_section(text, "User profile") or {}).get("profile", {})
    amount = re.search(r"(?:INR|USD|EUR)\s*([\d,]+(?:\.\d+)?)|([\d,]+(?:\.\d+)?)\s*(?:INR|USD|EUR)", goal)
    currency = re.search(r"\b(INR|USD|EUR)\b", goal)
    date = re.search(r"\b(\d{1,2}) ([A-Za-z]{3})[a-z]* (\d{4})\b", goal)
    receipt = re.search(r"Receipt file: (.+?)\s*$", goal, re.M)
    purpose = re.search(r"purpose '([^']+)'", goal)
    out = []
    for f in fields:
        label = str(f.get("label", ""))
        low = label.lower()
        value = None
        if low.startswith("full name"):
            value = profile.get("full name")
        elif low == "email":
            value = profile.get("email")
        elif low == "phone":
            value = profile.get("phone")
        elif low.startswith("team"):
            value = profile.get("team name")
        elif "category" in low and "taxi" in goal.lower():
            value = "Travel"
        elif "date" in low and date and date.group(2).lower() in MONTHS:
            value = f"{date.group(3)}-{MONTHS[date.group(2).lower()]:02d}-{int(date.group(1)):02d}"
        elif low == "amount" and amount:
            value = (amount.group(1) or amount.group(2)).replace(",", "")
        elif low == "currency" and currency:
            value = currency.group(1)
        elif low.startswith("receipt") and receipt:
            value = receipt.group(1)
        elif low == "purpose" and purpose:
            value = purpose.group(1)
        elif low.startswith("i confirm"):
            value = "yes"
        if value and "[REDACTED:" not in str(value):  # never type a redaction marker into a form
            out.append({"field": label, "value": str(value)})
    return out


# ---- setting up a rehearsal ----------------------------------------------------------------
def prepare(data_dir: str | Path | None = None) -> config_mod.Config:
    """A fresh rehearsal folder: a copy of demo/home, its own zones, profile, memory and key.
    Nothing in your real ~/.lantern is read or changed (except the server token, so the desktop app
    can connect as usual)."""
    data = Path(data_dir).expanduser() if data_dir else Path(tempfile.mkdtemp(prefix="lantern-demo-"))
    data.mkdir(parents=True, exist_ok=True)
    home = data / "home"
    if home.exists():
        raise DemoError(f"{home} already exists; give --demo-dir an empty folder")
    _copy_fixtures(DEMO_HOME, home)
    cfg = config_mod.load()   # keeps your browser settings (LANTERN_FORMS_HEADLESS, LANTERN_CHROMIUM)
    cfg.roots = [str(home)]
    for name in config_mod.PATH_FIELDS:
        default = Path(getattr(config_mod.Config, name))
        setattr(cfg, name, str(data / default.name))
    cfg.base_url = DESTINATION
    cfg.model, cfg.forms_model, cfg.forms_protocol, cfg.forms_coords = models.SPARK, models.WONDER, "native", ""
    cfg.forms_domains = []
    zones = {"zones": [{"path": str(home), "zone": "open"},
                       {"path": str(home / "Documents"), "zone": "writable"},
                       {"path": str(home / "Downloads"), "zone": "writable"},
                       {"path": str(home / "Private"), "zone": "private"}]}
    Path(cfg.zones_file).write_text(json.dumps(zones, indent=1), encoding="utf-8")
    Vault(cfg.profile_path).save(json.loads(DEMO_PROFILE.read_text(encoding="utf-8")))
    return cfg


def _copy_fixtures(src: Path, dst: Path) -> None:
    """Copy the repo's own demo/home into a brand-new folder. These are fixtures, not user files,
    so this is the one copy that doesn't go through policy.py (which guards *user* paths)."""
    for f in sorted(src.rglob("*")):
        target = dst / f.relative_to(src)
        if f.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif f.is_file() and not f.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(f.read_bytes())


def build(cfg: config_mod.Config, *, locate: Callable[[], dict] | None = None, client=None,
          transcriber: Callable | None = None, converter: Callable | None = None, workers: int = 0):
    """The rehearsal Core and channel hub (simulated phone only; voice notes 'transcribe' to the script).
    scripts/live_story.py passes a real model client, transcriber and converter to play the same
    story against the live Meta API."""
    from .channels import ChannelHub, FakeChannel
    from .core import Core
    data = Path(cfg.db_path).parent
    cipher = load_cipher(data, "file") if cfg.encrypt else None   # never touches your OS keychain
    memory = Memory(cfg.db_path, cipher)
    memory.remember("organiser email", ORGANISER)
    audit = AuditLog(cfg.audit_path, cipher)
    policy = Policy.load(cfg.zones_file, cfg.roots)
    email = EmailService(EmailSettings(mode="fake", from_addr="asha.rao@example.com"), policy, audit,
                         outbox_dir=Path(cfg.media_dir) / "outbox")
    # Rehearsal uses the offline stand-in for the private lane, unless a real local Glimmer is configured.
    from .router import FakeGlimmer, Router, RouterConfig, make_backend
    lane = RouterConfig.from_env(dict(os.environ))
    router = Router(policy, make_backend(lane, audit) if lane.mode == "openai" else FakeGlimmer(audit))
    model = client if client is not None else DemoModel(locate or (lambda: measure_form(cfg.chromium_path)))
    core = Core(cfg, model, memory=memory, audit=audit, policy=policy, email=email, router=router, workers=workers)
    core.demo = client is None

    def copy_audio(src: Path, dst: Path) -> Path:
        dst.write_bytes(src.read_bytes())
        return dst

    def script_transcript(wav, keywords=None) -> dict:  # the voice note says the first beat, every time
        return {"transcript": STORY[0].goal, "audioDurationMs": 0}
    converter = converter or copy_audio
    transcriber = transcriber or script_transcript
    phone = FakeChannel()
    hub = ChannelHub(core, [phone], transcriber=transcriber, converter=converter)
    return core, hub, phone


# ---- playing it in the terminal ------------------------------------------------------------
def _label(e: dict) -> str:
    name, r = e.get("name", ""), e.get("result") or {}
    if isinstance(r, dict) and r.get("error"):
        return f"{name}: {str(r['error'])[:100]}"
    labels = {
        "search_files": lambda: f"searched files ({r.get('count', '?')} found)",
        "fill_form": lambda: "form submitted and verified ✓" if r.get("verified") else f"form: {r.get('reason', '?')}",
        "send_email": lambda: "email sent, found in Sent ✓" if r.get("verified") else f"email: {r.get('reason', '?')}",
        "ask_private": lambda: "asked the private lane (file stays here)",
        "read_file": lambda: f"read {Path(str(r.get('path', ''))).name}",
        "which_projects_use": lambda: f"checked the project map ({len(r.get('projects', []))} hits)",
        "make_widget": lambda: "widget saved and verified ✓" if r.get("verified") else f"widget: {r.get('reason', '?')}",
        "organize_files": lambda: f"moved {r.get('moved', 0)} of {r.get('of', '?')} files ✓" if r.get("verified")
        else f"organize: {r.get('reason') or r.get('failed')}",
        "set_reminder": lambda: f"reminder set for {r.get('due')} ✓" if r.get("verified") else f"reminder: {r.get('reason')}",
        "list_dir": lambda: f"looked in {Path(str(r.get('path', ''))).name}",
    }
    return labels.get(name, lambda: name)()


def run_story(core, hub, phone, *, out: Callable[[str], None] = print, beats: list[Beat] | None = None,
              timeout: float = 180.0) -> dict:
    """Play the beats one by one; approvals are tapped 'Approve' on the simulated phone."""
    done: list[dict] = []
    lock = threading.Condition()

    def on_event(e: dict) -> None:
        kind = e.get("type")
        if kind == "tool" and e.get("name") != "finish":
            out(f"   · {_label(e)}  [{e.get('badge', '')}]")
        elif kind == "form" and e.get("phase") == "step":
            out(f"   · browser: {e.get('action', 'step')}  [{e.get('badge', '')}]")
        elif kind == "approval" and e.get("event") == "requested":
            out(f"   ⏸  needs your OK: {e['approval']['summary'][:160]}")
        elif kind == "done":
            with lock:
                done.append(e)
                lock.notify_all()
    unsubscribe = core.subscribe(on_event)
    tapped: set[str] = set()
    results = []
    try:
        for beat in beats or STORY:
            if beat.via == "voice":
                out(f"\n📱 voice note › “{beat.goal}”")
                hub.receive(phone.incoming(audio=b"rehearsal voice note"))
            else:
                out(f"\n💻 you › {beat.goal}")
                core.submit(beat.goal)
            with lock:
                start = len(done)
            end = time.time() + timeout
            while True:
                for a in core.bus.pending():
                    if a.id not in tapped:
                        tapped.add(a.id)
                        button = f"approve:{a.id}:{a.nonce}:{a.payload_hash[:8]}"
                        phone.note_owner(button=button)
                        hub.receive(phone.incoming(button=button))
                        out("   ✅ approved on the phone")
                with lock:
                    if len(done) > start or time.time() > end:
                        break
                    lock.wait(0.05)
            with lock:
                finished = done[start] if len(done) > start else {"status": "timeout", "reply": ""}
            out(f"🏮 {finished.get('reply') or finished.get('summary', '')}".rstrip())
            results.append({"goal": beat.goal, "status": finished.get("status"), "reply": finished.get("reply", "")})
        hub.wait_idle(5)
    finally:
        unsubscribe()
    summary = core.audit.summary()
    cloud, local = summary["cloud"], summary["local"]
    redactions = dict(cloud.get("redactions") or {})
    out(f"\n📋 Audit: {cloud['calls']} model calls would have gone to Meta's API "
        f"({cloud['bytes'] / 1024:.1f} KB; rehearsal, so nothing was sent) · {local['calls']} private-lane calls "
        f"stayed on this computer · redacted: {', '.join(f'{k} ×{v}' for k, v in redactions.items()) or 'nothing'}")
    cost = core.memory.total_cost()
    out(f"💸 Cost: ${cost:.2f} (the rehearsal model reports zero tokens)")
    return {"beats": results, "audit": summary, "cost_usd": cost, "phone": phone.messages()}
