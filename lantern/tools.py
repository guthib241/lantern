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
from pathlib import Path
from typing import Callable

from .approvals import ApprovalError, Gate, as_gate
from .memory import Memory
from .policy import PRIVATE, Policy, PolicyError
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
                 router=None):
        if not isinstance(policy, Policy):
            if not policy:
                raise ValueError("Lantern needs at least one allowed folder")
            policy = Policy.from_roots(policy)
        self.policy = policy
        self.memory = memory
        self.gate = as_gate(approve)
        self.router = router  # router.Router: the private lane (Muse Glimmer)
        self.tainted = False  # set once untrusted content reached the model in this task

    def begin_task(self) -> None:
        self.tainted = False

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
        self.tainted = True
        return {"path": str(p), "truncated": len(text) > MAX_READ_CHARS, **wrap(text[:MAX_READ_CHARS], str(p))}

    def ask_private(self, path: str, question: str) -> dict:
        """Ask the local model about a private file; only its scrubbed answer comes back."""
        from .router import PrivateLaneError
        if self.router is None:
            raise ToolError("the private lane (local Muse Glimmer) is not set up, so I can't read private files")
        try:
            result = self.router.ask_private(path, question)
            self.tainted = True  # the answer is derived from untrusted file content
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
]
TOOL_NAMES = {t["name"] for t in TOOL_SPECS}
