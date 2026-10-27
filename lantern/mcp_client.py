"""MCP client: tools from other MCP servers become *deferred* tools for Muse Spark.

Per dev.meta.ai/docs/tool-search: add {"type": "tool_search"} to `tools` and mark each tool to defer
with "defer_loading": true. The model then searches the deferred tools on the server side
(tool_search_call / tool_search_output items) and only loads the ones it needs, so a big catalog
costs nothing until it's used. Lantern's rules:
  - never send tool_search without at least one deferred tool (the API rejects it),
  - tool_choice stays "auto" (never set), function names have at most one dot: `server.tool`,
  - descriptions and results come from another program: hidden text is stripped, results are
    wrapped as untrusted and taint the task,
  - every call needs approval unless the user allowlisted that tool as read-only in mcp.json
    (a server saying "I'm read-only" is not trusted).

~/.lantern/mcp.json:
  {"servers": {"notes": {"command": "python", "args": ["-m", "my_notes_mcp"], "read_only": ["search"]},
               "tracker": {"url": "http://127.0.0.1:9000/mcp"}}}
Servers run in one background asyncio thread; the agent calls them synchronously.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .untrusted import strip_hidden

SERVER_NAME = re.compile(r"^[a-z0-9_-]{1,24}$")
MAX_DESCRIPTION = 600
MAX_RESULT_CHARS = 12_000


class MCPError(RuntimeError):
    pass


@dataclass
class ToolRef:
    function: str          # what the model calls: "server.tool"
    server: str
    tool: str              # the MCP server's own tool name
    description: str
    schema: dict
    read_only: bool = False


def function_name(server: str, tool: str) -> str:
    """'notes' + 'get.note v2' -> 'notes.get_note_v2' (one dot, safe characters, <= 64 chars)."""
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", tool).strip("_") or "tool"
    return f"{server}.{safe}"[:64]


def clean_schema(schema: Any) -> dict:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return {"type": "object", "properties": {}}
    return {k: v for k, v in schema.items() if k not in ("$schema", "title")}


def with_deferred(tools: list[dict], deferred: list[dict]) -> list[dict]:
    """Append deferred tools and tool_search, but only if there is at least one deferred tool."""
    deferred = [t for t in deferred if t.get("defer_loading") is True]
    if not deferred:
        return list(tools)
    return list(tools) + deferred + [{"type": "tool_search"}]


@dataclass
class MCPHub:
    """Keeps MCP sessions open in a background event loop."""
    servers: dict = field(default_factory=dict)   # name -> {"command", "args", "env", "url", "read_only"} or a server object
    connect_timeout: float = 20.0
    call_timeout: float = 60.0

    def __post_init__(self):
        self.tools: dict[str, ToolRef] = {}
        self.errors: dict[str, str] = {}
        self._clients: dict[str, Any] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop: asyncio.Event | None = None
        self._ready = threading.Event()

    @classmethod
    def from_file(cls, path: str | Path) -> "MCPHub":
        p = Path(path).expanduser()
        if not p.exists():
            return cls()
        return cls(servers=json.loads(p.read_text(encoding="utf-8")).get("servers", {}))

    # ---- lifecycle ---------------------------------------------------------------------
    def start(self) -> "MCPHub":
        if not self.servers or self._thread:
            return self
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="lantern-mcp", daemon=True)
        self._thread.start()
        asyncio.run_coroutine_threadsafe(self._main(), self._loop)
        if not self._ready.wait(self.connect_timeout):
            self.errors["*"] = "MCP servers didn't connect in time"
        return self

    async def _main(self) -> None:
        """One task owns every session (anyio scopes must be exited by the task that entered them)."""
        from mcp import Client, StdioServerParameters
        self._stop = asyncio.Event()
        async with contextlib.AsyncExitStack() as stack:
            for name, spec in self.servers.items():
                if not SERVER_NAME.match(name):
                    self.errors[name] = "server names: lowercase letters, digits, - and _ only"
                    continue
                try:
                    if isinstance(spec, dict) and spec.get("url"):
                        target = spec["url"]
                    elif isinstance(spec, dict):
                        target = StdioServerParameters(command=spec["command"], args=list(spec.get("args", [])),
                                                       env=spec.get("env"), cwd=spec.get("cwd"))
                    else:
                        target = spec  # an in-process server object (tests)
                    client = await asyncio.wait_for(stack.enter_async_context(Client(target)), self.connect_timeout)
                    listed = await client.list_tools()
                except Exception as e:  # one broken server must not take the others down
                    self.errors[name] = f"{type(e).__name__}: {str(e)[:160]}"
                    continue
                self._clients[name] = client
                allow = set(spec.get("read_only", [])) if isinstance(spec, dict) else set()
                for t in listed.tools:
                    fn = function_name(name, t.name)
                    desc = strip_hidden(t.description or t.name)[:MAX_DESCRIPTION]
                    self.tools[fn] = ToolRef(fn, name, t.name, f"[MCP server '{name}', untrusted] {desc}",
                                             clean_schema(t.input_schema), read_only=t.name in allow)
            self._ready.set()
            await self._stop.wait()

    def close(self) -> None:
        if self._loop and self._stop:
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

    # ---- for the agent -------------------------------------------------------------------
    def function_specs(self) -> list[dict]:
        return [{"type": "function", "name": t.function, "description": t.description, "parameters": t.schema,
                 "defer_loading": True} for t in sorted(self.tools.values(), key=lambda t: t.function)]

    def call(self, function: str, args: dict) -> dict:
        ref = self.tools.get(function)
        if ref is None or ref.server not in self._clients:
            raise MCPError(f"unknown MCP tool {function}")
        future = asyncio.run_coroutine_threadsafe(self._clients[ref.server].call_tool(ref.tool, args), self._loop)
        try:
            result = future.result(self.call_timeout)
        except TimeoutError as e:
            future.cancel()
            raise MCPError(f"{function} took longer than {self.call_timeout:.0f} s") from e
        except Exception as e:
            raise MCPError(f"{function} failed: {type(e).__name__}: {str(e)[:200]}") from e
        text = "\n".join(getattr(c, "text", "") or f"[{getattr(c, 'type', 'content')}]" for c in result.content or [])
        return {"is_error": bool(result.is_error), "text": text[:MAX_RESULT_CHARS],
                "structured": result.structured_content if len(json.dumps(result.structured_content or {}, default=str)) < 4000 else None}
