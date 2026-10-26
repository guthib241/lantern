"""Lantern's hands: the actions it can take on your computer.

Safety rules baked in here, not left to the model:
  1. Every file access goes through policy.py (privacy zones). Hidden paths are never
     listed, private files are described instead of read, and only writable zones change.
  2. Anything that changes your files (write, move) asks for approval first, through
     approvals.py: the approval is bound to a hash of the exact change and used once.
     After untrusted content (file text, private-lane answers) enters the task, even
     normally automatic tools like `remember` need approval (taint).
  3. Lantern never deletes. The worst it can do is move a file, and it asks first.
  4. After every change, the tool checks the result on disk and reports what it saw,
     so the agent verifies instead of trusting itself.
"""
from __future__ import annotations

import fnmatch
import hashlib
import threading
from pathlib import Path
from typing import Callable

from .approvals import ApprovalError, Gate, as_gate
from .memory import Memory
from .policy import PRIVATE, WRITABLE, Policy, PolicyError
from .untrusted import wrap

Approver = Callable[[str], bool]  # gets a plain-English description, returns yes/no

MAX_READ_CHARS = 20_000
MAX_RESULTS = 50
TEXT_EXTS = {
    ".txt", ".md", ".csv", ".json", ".py", ".js", ".ts", ".html", ".css",
    ".yaml", ".yml", ".toml", ".ini", ".log", ".xml", ".tex", ".rst",
}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".cache"}


class ToolError(Exception):
    """A problem the model should see and recover from."""


class Toolbox:
    def __init__(self, policy: Policy | list[str | Path], memory: Memory, approve: Approver | Gate,
                 router=None, forms=None, email=None, envmap=None, widgets=None):
        if not isinstance(policy, Policy):
            if not policy:
                raise ValueError("Lantern needs at least one allowed folder")
            policy = Policy.from_roots(policy)
        self.policy = policy
        self.memory = memory
        self.gate = as_gate(approve)
        self.router = router  # router.Router: the private lane (Muse Glimmer)
        self.forms = forms    # forms.FormService: Lantern's browser for form filling
        self.email = email    # email_tool.EmailService
        self.envmap = envmap  # envmap.EnvMap: projects, their envs and packages
        self.command_runner = None  # (argv, cwd, env, timeout) -> (exit code, output); default envmap.run_command
        self.widgets = widgets  # widgets.Widgets
        self.client = None      # the (audited) model client, for tools that write with a model (widgets)
        self.tainted = False  # set once untrusted content reached the model in this task
        self.untrusted_emails: set[str] = set()  # addresses seen only in files/web/email content
        self.task_id: int | None = None
        self.cancel = threading.Event()  # the agent swaps in its own (STOP / kill switch)
        self.on_event: Callable[[str, dict], None] = lambda kind, data: None

    def begin_task(self, task_id: int | None = None) -> None:
        self.tainted = False
        self.untrusted_emails = set()
        self.task_id = task_id

    def _saw_untrusted(self, text: str) -> None:
        """Untrusted text reached the model: taint the task and note any addresses in it."""
        from .email_tool import emails_in
        self.tainted = True
        self.untrusted_emails |= emails_in(text)

    def _approve(self, action: str, summary: str, payload: dict):
        """Ask the gate. Returns commit(payload) on yes, None on no."""
        if self.tainted:
            summary += "  [after reading untrusted content]"
        return self.gate.ask(action, summary, payload, tainted=self.tainted)

    @staticmethod
    def _commit(commit, payload: dict) -> dict | None:
        """Bind: the payload about to run must hash to what was approved."""
        try:
            commit(payload)
        except ApprovalError as e:
            return {"done": False, "reason": str(e)}
        return None

    @property
    def roots(self) -> list[Path]:
        return self.policy.roots

    # ---- path safety -------------------------------------------------------
    def _visible(self, path: str) -> tuple[Path, str]:
        try:
            return self.policy.check_visible(path or str(self.roots[0]))
        except PolicyError as e:
            raise ToolError(str(e)) from e

    # ---- read-only tools ---------------------------------------------------
    def list_folders(self) -> dict:
        return {"allowed_folders": [{"path": z["path"], "zone": z["zone"]}
                                    for z in self.policy.describe() if z["zone"] != "hidden"]}

    def list_dir(self, path: str = "") -> dict:
        p, zone = self._visible(path)
        try:
            children = self.policy.list_dir(p)
        except PolicyError as e:
            raise ToolError(str(e)) from e
        entries = []
        for child in children:
            if child.name.startswith("."):
                continue
            entries.append({
                "name": child.name + ("/" if child.is_dir() else ""),
                "size": child.stat().st_size if child.is_file() else None,
                "zone": self.policy.zone_of(child),
            })
            if len(entries) >= 200:
                break
        return {"path": str(p), "zone": zone, "entries": entries}

    def search_files(self, query: str, in_content: bool = False) -> dict:
        """Find files by name (glob or words), optionally inside text files too.
        Private files match by name only; their contents are never searched for the cloud."""
        q = query.strip()
        if not q:
            raise ToolError("search query is empty")
        words = [w.lower() for w in q.split()]
        is_glob = any(c in q for c in "*?[")
        hits = []
        for root in self.roots:
            for full, zone in self.policy.walk(root, SKIP_DIRS):
                lname = full.name.lower()
                matched = (
                    fnmatch.fnmatch(lname, q.lower()) if is_glob
                    else all(w in lname for w in words)
                )
                where = "name"
                if not matched and in_content and zone != PRIVATE and full.suffix.lower() in TEXT_EXTS:
                    try:
                        text = self.policy.read_text(full, limit=200_000).lower()
                        matched = all(w in text for w in words)
                        where = "content"
                    except (OSError, PolicyError):
                        pass
                if matched:
                    st = full.stat()
                    hits.append({"path": str(full), "matched_in": where, "zone": zone,
                                 "size": st.st_size, "modified": st.st_mtime})
        hits.sort(key=lambda h: h["modified"], reverse=True)
        return {"query": q, "count": len(hits), "results": hits[:MAX_RESULTS]}

    def read_file(self, path: str) -> dict:
        p, zone = self._visible(path)
        if not p.is_file():
            raise ToolError(f"{p} is not a file")
        if zone == PRIVATE:
            return {"path": str(p), "private": True, "description": self.policy.describe_file(p),
                    "note": "This file is in a private zone. Its contents stay on this computer; "
                            "use ask_private to have the local model answer about it."}
        if p.suffix.lower() not in TEXT_EXTS:
            return {"path": str(p), "note": f"binary file ({p.suffix}), {p.stat().st_size} bytes; cannot show text"}
        text = self.policy.read_text(p, limit=MAX_READ_CHARS + 1)
        self._saw_untrusted(text)
        return {"path": str(p), "truncated": len(text) > MAX_READ_CHARS, **wrap(text[:MAX_READ_CHARS], str(p))}

    def ask_private(self, path: str, question: str) -> dict:
        """Ask the local model about a private file; only its scrubbed answer comes back."""
        from .router import PrivateLaneError
        if self.router is None:
            raise ToolError("the private lane (local Muse Glimmer) is not set up, so I can't read private files")
        try:
            result = self.router.ask_private(path, question)
            self._saw_untrusted(result.get("answer", ""))  # derived from untrusted file content
            return result
        except PrivateLaneError as e:
            raise ToolError(f"{e}. Private files never go to the cloud, so I can't answer this right now.") from e

    # ---- tools that change things (approval + verify) ----------------------
    def write_file(self, path: str, content: str, overwrite: bool = False) -> dict:
        try:
            p = self.policy.check_writable(path)
        except PolicyError as e:
            raise ToolError(str(e)) from e
        if p.exists() and not overwrite:
            raise ToolError(f"{p} already exists; set overwrite=true if you mean to replace it")
        verb = "Replace" if p.exists() else "Create"

        def plan(text: str) -> dict:
            return {"path": str(p), "sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "chars": len(text), "overwrite": p.exists()}
        commit = self._approve("write_file", f"{verb} file {p} ({len(content)} characters)", plan(content))
        if commit is None:
            return {"done": False, "reason": "user declined"}
        if refused := self._commit(commit, plan(content)):
            return refused
        self.policy.write_text(p, content)
        ok = p.is_file() and self.policy.read_text(p) == content  # verify on disk
        return {"done": ok, "path": str(p), "verified": ok}

    def move_file(self, source: str, destination: str) -> dict:
        try:
            src = self.policy.check_writable(source)
            dst = self.policy.check_writable(destination)
        except PolicyError as e:
            raise ToolError(str(e)) from e
        if not src.exists():
            raise ToolError(f"{src} does not exist")
        if dst.is_dir():
            dst = dst / src.name
        if dst.exists():
            raise ToolError(f"{dst} already exists; pick another name")
        payload = {"from": str(src), "to": str(dst)}
        commit = self._approve("move_file", f"Move {src} -> {dst}", payload)
        if commit is None:
            return {"done": False, "reason": "user declined"}
        if refused := self._commit(commit, {"from": str(src), "to": str(dst)}):
            return refused
        self.policy.move(src, dst)
        ok = dst.exists() and not src.exists()  # verify on disk
        return {"done": ok, "from": str(src), "to": str(dst), "verified": ok}

    # ---- forms (computer use) ------------------------------------------------
    def fill_form(self, url: str, goal: str) -> dict:
        """Fill a web form in Lantern's own browser; Submit always needs approval."""
        from .forms import FormError
        if self.forms is None:
            raise ToolError("form filling isn't set up on this computer")
        try:
            result = self.forms.fill(url, goal, approve=self._approve, commit=self._commit,
                                     task_id=self.task_id, cancel=self.cancel, on_event=self.on_event)
        except FormError as e:
            raise ToolError(str(e)) from e
        # labels and the confirmation text come from a web page
        self._saw_untrusted((result.get("confirmation") or {}).get("content", ""))
        return result

    # ---- email -----------------------------------------------------------------
    def send_email(self, to: list[str], subject: str, body: str, attachments: list[str] | None = None,
                   cc: list[str] | None = None) -> dict:
        """Send an email after approval; verified by finding it in the Sent folder."""
        from .email_tool import EmailError, emails_in
        if self.email is None:
            raise ToolError("email isn't set up on this computer")
        known: set[str] = set()  # addresses the user gave: in the request or remembered facts
        for value in self.memory.facts().values():
            known |= emails_in(value)
        if self.task_id:
            try:
                known |= emails_in(self.memory.get_task(self.task_id).goal)
            except KeyError:
                pass
        try:
            return self.email.send(to, subject, body, attachments, cc, approve=self._approve, commit=self._commit,
                                   untrusted_emails=self.untrusted_emails, known_emails=known,
                                   task_id=self.task_id)
        except EmailError as e:
            raise ToolError(str(e)) from e

    # ---- projects (environment map) ------------------------------------------
    def _envmap(self):
        if self.envmap is None:
            raise ToolError("the project map isn't set up")
        return self.envmap

    def _project(self, path: str):
        em = self._envmap()
        try:
            project = em.get(path)
        except PolicyError as e:
            raise ToolError(str(e)) from e
        if project is None:
            matches = em.find(path)
            if len(matches) == 1:
                project = matches[0]
            elif matches:
                raise ToolError("several projects match: " + ", ".join(p.path for p in matches[:10]))
        if project is None:
            raise ToolError(f"no project found for '{path}'; try find_project")
        return project

    def find_project(self, name: str) -> dict:
        hits = self._envmap().find(name)
        return {"query": name, "count": len(hits), "projects": [p.summary() for p in hits[:30]]}

    def project_info(self, path: str) -> dict:
        p = self._project(path)
        info = p.summary()
        info["sources"] = p.sources
        info["declared"] = dict(list(p.declared.items())[:150])
        info["scripts"] = p.scripts
        envs = []
        for e in p.envs:
            missing = [n for n in p.declared if not n.startswith("npm:") and n not in e["packages"]]
            envs.append({"path": e["path"], "python": e["python"], "installed": len(e["packages"]),
                         "declared_but_missing": missing[:50]})
        info["envs"] = envs
        return info

    def which_projects_use(self, package: str) -> dict:
        if not package.strip():
            raise ToolError("package name is empty")
        return self._envmap().which_use(package)

    def run_in_project(self, path: str, command: str) -> dict:
        """Run an allowlisted command in a project's own env, after approval."""
        from .envmap import CommandError, plan_command, run_command, safe_env
        project = self._project(path)
        if project.private:
            raise ToolError("that project is in a private zone; run commands there yourself")
        if self.policy.zone_of(project.path) != WRITABLE:
            raise ToolError(f"{project.name} is not in a writable zone, and commands can change files there")
        try:
            plan = plan_command(project, command)
        except CommandError as e:
            raise ToolError(str(e)) from e
        payload = {"cwd": plan["cwd"], "argv": plan["argv"]}
        where = f" using {plan['env']}" if plan["env"] else ""
        commit = self._approve("run_in_project", f"Run `{plan['display']}` in {project.name} ({plan['cwd']}){where}", payload)
        if commit is None:
            return {"done": False, "reason": "user declined"}
        if refused := self._commit(commit, payload):
            return refused
        runner = self.command_runner or run_command
        code, out = runner(plan["argv"], plan["cwd"], safe_env(plan["env"]), 600 if plan["kind"] == "pip_install" else 300)
        tail = out[-4000:]
        self._saw_untrusted(tail)
        lines = [ln.strip() for ln in tail.splitlines() if ln.strip()]
        result = {"project": project.name, "command": plan["display"], "exit_code": code, "ok": code == 0,
                  "summary": lines[-1][:200] if lines else "", **wrap(tail, f"{project.name}: {plan['display']}", kind="command")}
        if plan["kind"] == "pip_install":  # verify in the env itself, not from pip's output
            self._envmap().projects(refresh=True)
            fresh = self._envmap().get(project.path)
            installed = bool(fresh) and any(plan["package"] in e["packages"] for e in fresh.envs)
            result.update(done=installed, verified=installed, package=plan["package"])
        return result

    # ---- widgets ----------------------------------------------------------------
    def make_widget(self, name: str, description: str) -> dict:
        """Have Muse Spark write a small sandboxed widget; save it after approval."""
        from . import models
        from .widgets import WidgetError
        if self.widgets is None or self.client is None:
            raise ToolError("widgets aren't set up")

        def usage(model: str, u) -> None:
            cost = models.token_cost(model, u)
            self.memory.log_usage(self.task_id, model, u.input_tokens, u.cached_tokens, u.output_tokens, cost)
            self.on_event("usage", {"model": model, "badge": models.badge(model), "input_tokens": u.input_tokens,
                                    "cached_tokens": u.cached_tokens, "output_tokens": u.output_tokens, "cost_usd": cost})
        try:
            html, attempts = self.widgets.generate(self.client, description, on_usage=usage)
            result = self.widgets.save(name, html, approve=self._approve, commit=self._commit)
        except WidgetError as e:
            raise ToolError(str(e)) from e
        return {**result, "attempts": attempts, "model": models.badge(models.SPARK)}

    # ---- memory tools ------------------------------------------------------
    def remember(self, key: str, value: str) -> dict:
        if self.tainted:  # untrusted text must not be able to plant "facts" silently
            payload = {"key": key, "value": value}
            commit = self._approve("remember", f"Remember '{key}' = '{value}'", payload)
            if commit is None:
                return {"done": False, "reason": "user declined"}
            if refused := self._commit(commit, payload):
                return refused
        self.memory.remember(key, value)
        return {"remembered": {key: value}}

    def forget(self, key: str) -> dict:
        if self.tainted:
            commit = self._approve("forget", f"Forget '{key}'", {"key": key})
            if commit is None:
                return {"done": False, "reason": "user declined"}
            if refused := self._commit(commit, {"key": key}):
                return refused
        return {"forgot": key, "existed": self.memory.forget(key)}

    # ---- dispatch ----------------------------------------------------------
    def call(self, name: str, args: dict) -> dict:
        fn = getattr(self, name, None)
        if name.startswith("_") or name not in TOOL_NAMES or fn is None:
            raise ToolError(f"unknown tool {name}")
        try:
            return fn(**args)
        except TypeError as e:
            raise ToolError(f"bad arguments for {name}: {e}") from e
        except PolicyError as e:
            raise ToolError(str(e)) from e
        except OSError as e:
            raise ToolError(f"{name} failed: {e}") from e


def _fn(name: str, description: str, props: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": props, "required": required,
                       "additionalProperties": False},
    }


S = {"type": "string"}
TOOL_SPECS = [
    _fn("list_folders", "List the folders Lantern may see and their privacy zone (private / open / writable).", {}, []),
    _fn("list_dir", "List files and subfolders in a folder.", {"path": {**S, "description": "Folder path; empty for the main folder"}}, []),
    _fn("search_files",
        "Find files by name words or a glob like '*.pdf'. Set in_content to also search inside text files. Results are newest first.",
        {"query": {**S, "description": "Words in the file name, or a glob"},
         "in_content": {"type": "boolean", "description": "Also search inside text files"}},
        ["query"]),
    _fn("read_file", "Read a text file. Private-zone files return only a description.", {"path": S}, ["path"]),
    _fn("ask_private",
        "Ask a question about a file in a private zone. A local model on this computer reads it and returns a short answer; the file never leaves the computer.",
        {"path": S, "question": S}, ["path", "question"]),
    _fn("write_file", "Create (or with overwrite=true, replace) a text file. The user is asked to approve.",
        {"path": S, "content": S, "overwrite": {"type": "boolean"}}, ["path", "content"]),
    _fn("move_file", "Move or rename a file. Destination can be a folder. The user is asked to approve.",
        {"source": S, "destination": S}, ["source", "destination"]),
    _fn("remember", "Save a lasting fact about the user or their setup, e.g. key='manager email'.",
        {"key": S, "value": S}, ["key", "value"]),
    _fn("forget", "Delete a remembered fact.", {"key": S}, ["key"]),
    _fn("fill_form",
        "Fill a web form in Lantern's own browser from the user's saved profile and the goal. Lantern never types "
        "passwords, one-time codes, card or ID numbers and stops at CAPTCHAs. Submitting always asks the user first; "
        "the result says whether the confirmation page was verified.",
        {"url": {**S, "description": "The form's URL, or the name of a saved form such as 'hackathon reimbursement'"},
         "goal": {**S, "description": "What to fill in, with details the profile doesn't have (amount, date, purpose, receipt file path)"}},
        ["url", "goal"]),
    _fn("send_email",
        "Send an email. The user approves the exact recipients, subject, body and attachments first; "
        "the result says whether it was found in the Sent folder (verified).",
        {"to": {"type": "array", "items": S, "description": "Recipient addresses"},
         "subject": S, "body": {**S, "description": "Plain-text body"},
         "attachments": {"type": "array", "items": S, "description": "File paths to attach"},
         "cc": {"type": "array", "items": S}},
        ["to", "subject", "body"]),
    _fn("find_project", "Find software projects on this computer by name. Uses Lantern's local project map.",
        {"name": S}, ["name"]),
    _fn("project_info", "One project's languages, declared dependencies, virtual envs, installed package counts "
        "and declared-but-missing packages.", {"path": {**S, "description": "Project folder or name"}}, ["path"]),
    _fn("which_projects_use", "Which projects declare or have installed a package, e.g. 'torch' (prefix npm: for Node "
        "packages). Answered from the local project map; no files are read.", {"package": S}, ["package"]),
    _fn("run_in_project", "Run an allowlisted command inside a project's own environment (never the global Python): "
        "pytest / python -m pytest [-q -x -k EXPR paths], npm test, or pip install <one package>. The user approves first.",
        {"path": {**S, "description": "Project folder or name"}, "command": S}, ["path", "command"]),
    _fn("make_widget", "Write a small widget for the desktop panel (a timer, counter, checklist...). It runs sandboxed "
        "with no network; the user approves saving it.",
        {"name": {**S, "description": "Short name, e.g. 'water-timer'"},
         "description": {**S, "description": "What it should do and look like"}}, ["name", "description"]),
]
TOOL_NAMES = {t["name"] for t in TOOL_SPECS}
