"""Lantern's hands: the actions it can take on your computer.

Safety rules baked in here, not left to the model:
  1. Every path must sit inside a folder you allowed (config.roots). No escaping with ../ or symlinks.
  2. Anything that changes your files (write, move) asks for approval first.
  3. Lantern never deletes. The worst it can do is move a file, and it asks first.
  4. After every change, the tool checks the result on disk and reports what it saw,
     so the agent verifies instead of trusting itself.
"""
from __future__ import annotations

import fnmatch
import os
import shutil
from pathlib import Path
from typing import Callable

from .memory import Memory

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
    def __init__(self, roots: list[str | Path], memory: Memory, approve: Approver):
        if not roots:
            raise ValueError("Lantern needs at least one allowed folder")
        self.roots = [Path(r).expanduser().resolve() for r in roots]
        self.memory = memory
        self.approve = approve

    # ---- path safety -------------------------------------------------------
    def _resolve(self, path: str) -> Path:
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = self.roots[0] / p
        p = p.resolve()
        for root in self.roots:
            if p == root or root in p.parents:
                return p
        raise ToolError(
            f"'{path}' is outside the folders I'm allowed to touch: "
            + ", ".join(str(r) for r in self.roots)
        )

    def _show(self, p: Path) -> str:
        for root in self.roots:
            if p == root or root in p.parents:
                return str(p)
        return str(p)

    # ---- read-only tools ---------------------------------------------------
    def list_folders(self) -> dict:
        return {"allowed_folders": [str(r) for r in self.roots]}

    def list_dir(self, path: str = "") -> dict:
        p = self._resolve(path or str(self.roots[0]))
        if not p.is_dir():
            raise ToolError(f"{p} is not a folder")
        entries = []
        for child in sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower())):
            if child.name.startswith("."):
                continue
            entries.append({
                "name": child.name + ("/" if child.is_dir() else ""),
                "size": child.stat().st_size if child.is_file() else None,
            })
            if len(entries) >= 200:
                break
        return {"path": str(p), "entries": entries}

    def search_files(self, query: str, in_content: bool = False) -> dict:
        """Find files by name (glob or words), optionally inside text files too."""
        q = query.strip()
        if not q:
            raise ToolError("search query is empty")
        words = [w.lower() for w in q.split()]
        is_glob = any(c in q for c in "*?[")
        hits = []
        for root in self.roots:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
                for name in filenames:
                    full = Path(dirpath) / name
                    lname = name.lower()
                    matched = (
                        fnmatch.fnmatch(lname, q.lower()) if is_glob
                        else all(w in lname for w in words)
                    )
                    where = "name"
                    if not matched and in_content and full.suffix.lower() in TEXT_EXTS:
                        try:
                            text = full.read_text(errors="ignore")[:200_000].lower()
                            matched = all(w in text for w in words)
                            where = "content"
                        except OSError:
                            pass
                    if matched:
                        st = full.stat()
                        hits.append({"path": str(full), "matched_in": where,
                                     "size": st.st_size, "modified": st.st_mtime})
        hits.sort(key=lambda h: h["modified"], reverse=True)
        return {"query": q, "count": len(hits), "results": hits[:MAX_RESULTS]}

    def read_file(self, path: str) -> dict:
        p = self._resolve(path)
        if not p.is_file():
            raise ToolError(f"{p} is not a file")
        if p.suffix.lower() not in TEXT_EXTS:
            return {"path": str(p), "note": f"binary file ({p.suffix}), {p.stat().st_size} bytes; cannot show text"}
        text = p.read_text(errors="replace")
        return {"path": str(p), "truncated": len(text) > MAX_READ_CHARS, "text": text[:MAX_READ_CHARS]}

    # ---- tools that change things (approval + verify) ----------------------
    def write_file(self, path: str, content: str, overwrite: bool = False) -> dict:
        p = self._resolve(path)
        if p.exists() and not overwrite:
            raise ToolError(f"{p} already exists; set overwrite=true if you mean to replace it")
        verb = "Replace" if p.exists() else "Create"
        if not self.approve(f"{verb} file {p} ({len(content)} characters)"):
            return {"done": False, "reason": "user declined"}
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        ok = p.is_file() and p.read_text() == content  # verify on disk
        return {"done": ok, "path": str(p), "verified": ok}

    def move_file(self, source: str, destination: str) -> dict:
        src = self._resolve(source)
        dst = self._resolve(destination)
        if not src.exists():
            raise ToolError(f"{src} does not exist")
        if dst.is_dir():
            dst = dst / src.name
        if dst.exists():
            raise ToolError(f"{dst} already exists; pick another name")
        if not self.approve(f"Move {src} -> {dst}"):
            return {"done": False, "reason": "user declined"}
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        ok = dst.exists() and not src.exists()  # verify on disk
        return {"done": ok, "from": str(src), "to": str(dst), "verified": ok}

    # ---- memory tools ------------------------------------------------------
    def remember(self, key: str, value: str) -> dict:
        self.memory.remember(key, value)
        return {"remembered": {key: value}}

    def forget(self, key: str) -> dict:
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
    _fn("list_folders", "List the folders Lantern is allowed to work in.", {}, []),
    _fn("list_dir", "List files and subfolders in a folder.", {"path": {**S, "description": "Folder path; empty for the main folder"}}, []),
    _fn("search_files",
        "Find files by name words or a glob like '*.pdf'. Set in_content to also search inside text files. Results are newest first.",
        {"query": {**S, "description": "Words in the file name, or a glob"},
         "in_content": {"type": "boolean", "description": "Also search inside text files"}},
        ["query"]),
    _fn("read_file", "Read a text file.", {"path": S}, ["path"]),
    _fn("write_file", "Create (or with overwrite=true, replace) a text file. The user is asked to approve.",
        {"path": S, "content": S, "overwrite": {"type": "boolean"}}, ["path", "content"]),
    _fn("move_file", "Move or rename a file. Destination can be a folder. The user is asked to approve.",
        {"source": S, "destination": S}, ["source", "destination"]),
    _fn("remember", "Save a lasting fact about the user or their setup, e.g. key='manager email'.",
        {"key": S, "value": S}, ["key", "value"]),
    _fn("forget", "Delete a remembered fact.", {"key": S}, ["key"]),
]
TOOL_NAMES = {t["name"] for t in TOOL_SPECS}
