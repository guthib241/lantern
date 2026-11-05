"""Lantern as an MCP server (stdio): another AI app can use Lantern's safe tools. Ready for Meta AI Connectors.

    python -m lantern --mcp        # speak MCP on stdin/stdout

Exposed tools, all read-only and all through the same policy as Lantern itself:
  list_folders, list_dir, search_files, read_file (private files: description only),
  ask_private (the local model answers; only its scrubbed answer comes out),
  find_project, project_info, which_projects_use,
  submit_task / task_status: forwarded to the running Lantern (`python -m lantern --serve`), so
  anything that changes the world goes through its approval bus (desktop / WhatsApp buttons).
Every result leaves this computer as far as Lantern knows, so it is scrubbed (secrets, ID numbers)
and recorded in the audit log ("What left my laptop today"), like any cloud call.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError as MCPToolError

from .scrubber import DEFAULT as SCRUBBER
from .tools import Toolbox, ToolError

INSTRUCTIONS = ("Lantern is a private assistant on the user's computer. These tools read only what the user's "
                "privacy zones allow; private files are answered by a local model. To change anything, use "
                "submit_task: the user approves it on their desktop or phone.")


class LocalLantern:
    """Talks to the running Lantern server on 127.0.0.1 with its per-install token."""

    def __init__(self, base: str, token: str, timeout: float = 15):
        self.base, self.token, self.timeout = base.rstrip("/"), token, timeout

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(self.base + path, method=method, data=json.dumps(body).encode() if body else None,
                                     headers={"X-Lantern-Token": self.token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raise ToolError(f"Lantern said {e.code}: {e.read()[:200].decode(errors='replace')}") from e
        except (urllib.error.URLError, OSError) as e:
            raise ToolError("Lantern isn't running; start it with `python -m lantern --serve`") from e


Forward = Callable[[str, str, dict | None], dict]


def build_server(toolbox: Toolbox, *, audit=None, forward: Forward | None = None) -> MCPServer:
    server = MCPServer("lantern", instructions=INSTRUCTIONS)

    def out(name: str, fn: Callable[[], Any]) -> dict:
        try:
            result = fn()
        except ToolError as e:
            raise MCPToolError(str(e)) from e
        clean, found = SCRUBBER.scrub_obj(json.loads(json.dumps(result, default=str)))
        if audit is not None:
            body = json.dumps(clean, ensure_ascii=False).encode()
            audit.record(lane="cloud", destination="mcp-client", endpoint=f"mcp.{name}", model="mcp",
                         payload=body, redactions=found, preview=body[:300].decode(errors="replace"))
        return clean

    def forwarded(method: str, path: str, body: dict | None = None) -> dict:
        if forward is None:
            raise MCPToolError("task forwarding isn't set up")
        try:
            return forward(method, path, body)
        except ToolError as e:
            raise MCPToolError(str(e)) from e

    @server.tool(description="The folders Lantern may see and their privacy zone.")
    def list_folders() -> dict:
        return out("list_folders", toolbox.list_folders)

    @server.tool(description="List a folder (hidden folders never appear).")
    def list_dir(path: str = "") -> dict:
        return out("list_dir", lambda: toolbox.list_dir(path))

    @server.tool(description="Find files by name words or a glob; in_content also searches inside open text files.")
    def search_files(query: str, in_content: bool = False) -> dict:
        return out("search_files", lambda: toolbox.search_files(query, in_content))

    @server.tool(description="Read a text file from an open folder. Private files return only a description.")
    def read_file(path: str) -> dict:
        return out("read_file", lambda: toolbox.read_file(path))

    @server.tool(description="Ask about a private file: Lantern's local model reads it; only a short scrubbed answer comes back.")
    def ask_private(path: str, question: str) -> dict:
        # The plain toolbox answers with the private lane's text (S0's handles exist only on the scheduler's path);
        # claude/real-run's $p placeholders, which this once unwrapped, were superseded by S0 when combined.
        return out("ask_private", lambda: toolbox.ask_private(path, question))

    @server.tool(description="Find software projects by name.")
    def find_project(name: str) -> dict:
        return out("find_project", lambda: toolbox.find_project(name))

    @server.tool(description="A project's languages, dependencies and virtual envs.")
    def project_info(path: str) -> dict:
        return out("project_info", lambda: toolbox.project_info(path))

    @server.tool(description="Which projects declare or have installed a package, e.g. torch.")
    def which_projects_use(package: str) -> dict:
        return out("which_projects_use", lambda: toolbox.which_projects_use(package))

    @server.tool(description="Ask Lantern to do something (fill a form, send an email, move files...). "
                             "The user approves every change on their desktop or phone. Returns a task id.")
    def submit_task(goal: str) -> dict:
        r = forwarded("POST", "/task", {"goal": goal, "source": "mcp"})
        return {"task_id": r.get("task_id"), "note": "Queued. The user approves any change on their desktop or phone."}

    @server.tool(description="Status and summary of a task started with submit_task.")
    def task_status(task_id: int) -> dict:
        t = forwarded("GET", f"/tasks/{int(task_id)}", None)
        return out("task_status", lambda: {k: t.get(k) for k in ("id", "goal", "status", "summary")})

    return server
