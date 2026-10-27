"""Where geometry comes from: real FreeCAD (a FreeCADCmd worker process) or the fake.

    make_backend("auto")    FreeCAD if FreeCADCmd is found, else an error that says how to install it
    make_backend("freecad") the real thing (laptop)
    make_backend("fake")    the stand-in (tests, cloud sandbox, rehearsals)

The FreeCAD worker is one long-lived process speaking JSON lines (worker.py), so Lantern's own
Python doesn't need FreeCAD importable, and FreeCAD's Python doesn't need the MCP package.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from .fake import FakeBackend
from .render import render

WORKER = Path(__file__).with_name("worker.py")
PREFIX = "@@LANTERN "
CANDIDATES = [   # FreeCAD 1.x renamed FreeCADCmd to freecadcmd; both are tried
    "freecadcmd", "FreeCADCmd", "freecad.cmd",
    r"C:\Program Files\FreeCAD 1.1\bin\freecadcmd.exe", r"C:\Program Files\FreeCAD 1.0\bin\freecadcmd.exe",
    r"C:\Program Files\FreeCAD 0.21\bin\FreeCADCmd.exe",
    "/Applications/FreeCAD.app/Contents/Resources/bin/freecadcmd",
    "/Applications/FreeCAD.app/Contents/MacOS/FreeCADCmd",
]


class BackendError(RuntimeError):
    """FreeCAD missing, crashed, timed out, or refused an operation. Safe to show."""


def find_freecadcmd(env: dict | None = None) -> str | None:
    env = os.environ if env is None else env
    if env.get("LANTERN_FREECAD_CMD"):
        p = env["LANTERN_FREECAD_CMD"]
        return p if Path(p).exists() or shutil.which(p) else None
    for c in CANDIDATES:
        found = shutil.which(c) or (c if Path(c).is_absolute() and Path(c).exists() else None)
        if found:
            return found
    return None


class FreeCADBackend:
    """Talks to worker.py running inside FreeCADCmd."""

    name = "freecad"

    def __init__(self, cmd: str, *, timeout: float = 120.0, start_timeout: float = 60.0, popen=subprocess.Popen):
        self.cmd, self.timeout, self.start_timeout, self.popen = cmd, timeout, start_timeout, popen
        self.proc = None
        self._lines: queue.Queue = queue.Queue()
        self._next = 1
        self._lock = threading.Lock()
        self.version = ""

    # ---- process ------------------------------------------------------------------------
    def start(self) -> "FreeCADBackend":
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        self.proc = self.popen([self.cmd, str(WORKER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", env=env,
                               bufsize=1)
        # A fresh queue per process: a stopped worker's late "EOF" must never reach its replacement.
        self._lines = queue.Queue()
        threading.Thread(target=self._read, args=(self.proc, self._lines), daemon=True,
                         name="lantern-freecad-out").start()
        hello = self._reply(0, self.start_timeout)
        self.version = (hello.get("result") or {}).get("version", "")
        return self

    @staticmethod
    def _read(proc, lines: queue.Queue) -> None:
        for line in proc.stdout:
            if line.startswith(PREFIX):
                try:
                    lines.put(json.loads(line[len(PREFIX):]))
                except json.JSONDecodeError:
                    pass
        lines.put(None)   # EOF: this worker exited

    def _reply(self, want_id: int, timeout: float) -> dict:
        while True:
            try:
                msg = self._lines.get(timeout=timeout)
            except queue.Empty:
                self.kill()
                raise BackendError(f"FreeCAD didn't answer within {timeout:.0f} s (stopped it)") from None
            if msg is None:
                raise BackendError("FreeCAD stopped unexpectedly")
            if msg.get("id") == want_id:
                return msg

    def call(self, op: str, args: dict) -> dict:
        if op == "render":   # render here, from FreeCAD's tessellation
            tris = self.call("mesh", {"names": args.get("names")} if args.get("names") else {})["triangles"]
            return {"png": render([tuple(map(tuple, t)) for t in tris]), "backend": self.name}
        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                self.start()   # first use, or restart once after a crash
            msg_id, self._next = self._next, self._next + 1
            try:
                self.proc.stdin.write(json.dumps({"id": msg_id, "op": op, "args": args}) + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                raise BackendError(f"FreeCAD isn't running ({e})") from e
            msg = self._reply(msg_id, self.timeout)
        if not msg.get("ok"):
            raise BackendError(msg.get("error") or "FreeCAD refused the operation")
        return msg["result"]

    def kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(5)

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.write(json.dumps({"op": "quit"}) + "\n")
                self.proc.stdin.flush()
                self.proc.wait(5)
            except Exception:
                self.proc.kill()


def make_backend(kind: str = "auto", env: dict | None = None):
    kind = (kind or "auto").lower()
    if kind == "fake":
        return FakeBackend()
    cmd = find_freecadcmd(env)
    if cmd is None:
        if kind == "freecad":
            raise BackendError("FreeCAD isn't installed (or FreeCADCmd isn't on PATH): install FreeCAD 1.0+ and set "
                               "LANTERN_FREECAD_CMD to its freecadcmd, see docs/FREECAD.md")
        return FakeBackend() if kind == "auto-or-fake" else _missing()
    return FreeCADBackend(cmd)


def _missing():
    raise BackendError("FreeCAD isn't installed: see docs/FREECAD.md (or set LANTERN_FREECAD_BACKEND=fake to rehearse)")


def python_for_server() -> str:
    """The interpreter that runs the MCP server: Lantern's own."""
    return sys.executable
