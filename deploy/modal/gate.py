"""The private lane's front door: a tiny ASGI app that checks the bearer token.

It runs on a small CPU container. The GPU container that holds Muse Glimmer has no
web endpoint at all; the gate reaches it only through a Modal function call
(`upstream`). So a request without the right token is turned away here and never
wakes (or pays for) a GPU.

Rules, enforced in code:
  - every request needs `Authorization: Bearer <LANTERN_PRIVATE_TOKEN>`, compared in
    constant time; anything else gets 401 before the body is read
  - only GET /v1/models and POST /v1/chat/completions exist; everything else is 404
  - bodies over MAX_BODY_BYTES are refused (413), non-JSON chat bodies are refused (400)
  - chat requests are normalised: Glimmer's sampling (temperature 1.0, top_p 0.95,
    top_k 64) fills anything missing, temperature <= 0 is raised to 1.0 (greedy
    decoding loops), one tool call per turn, max_tokens is capped, no streaming
  - nothing about a request or response body is logged or printed, ever

Standard library only, so tests can drive it without Modal or a network.
"""
from __future__ import annotations

import hmac
import json
from typing import Any, Awaitable, Callable

from glimmer_config import MAX_TOKENS_CAP, SAMPLING, SERVED_MODEL, validate_token

ALLOWED = frozenset({("GET", "/v1/models"), ("POST", "/v1/chat/completions")})
MAX_BODY_BYTES = 1_000_000

# (method, path, body) -> (status, JSON body bytes)
Upstream = Callable[[str, str, bytes], Awaitable[tuple[int, bytes]]]


class GateError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def bearer_ok(header: str | None, token: str) -> bool:
    """True only for exactly `Bearer <token>` (scheme case-insensitive), compared in constant time."""
    if not header or not token:
        return False
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer" or not presented:
        return False
    return hmac.compare_digest(presented.strip().encode(), token.encode())


def prepare_chat(body: bytes) -> bytes:
    """Validate a chat request and apply Glimmer's serving rules. Returns the body to forward."""
    try:
        req = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise GateError(400, "body must be JSON") from None
    if not isinstance(req, dict) or not isinstance(req.get("messages"), list) or not req["messages"]:
        raise GateError(400, "messages must be a non-empty list")
    if req.get("stream"):
        raise GateError(400, "streaming isn't supported by this server; send stream=false")
    if req.get("n", 1) != 1:
        raise GateError(400, "n must be 1")
    req["model"] = SERVED_MODEL
    for key, value in SAMPLING.items():
        if not isinstance(req.get(key), (int, float)) or isinstance(req.get(key), bool):
            req[key] = value
    if req["temperature"] <= 0:
        req["temperature"] = SAMPLING["temperature"]   # never greedy: Glimmer loops at temperature 0
    req["parallel_tool_calls"] = False                  # one tool call per turn
    limit = req.get("max_tokens") or req.get("max_completion_tokens") or 1024
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise GateError(400, "max_tokens must be a positive whole number")
    req.pop("max_completion_tokens", None)
    req["max_tokens"] = min(limit, MAX_TOKENS_CAP)
    return json.dumps(req).encode()


def _error_body(message: str) -> bytes:
    return json.dumps({"error": {"message": message, "type": "lantern_gate"}}).encode()


class Gate:
    """ASGI app. `token` comes from the lantern-private Modal Secret; `upstream` calls the GPU."""

    def __init__(self, token: str | None, upstream: Upstream):
        self._token = validate_token(token)   # refuse to start rather than run open
        self._upstream = upstream

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope["type"] != "http":   # websockets etc.: not here
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        try:
            status, body = await self._handle(scope, receive)
        except GateError as e:
            status, body = e.status, _error_body(e.message)
        except Exception:   # upstream down or crashed; no details, they could echo content
            status, body = 502, _error_body("the private model isn't answering")
        headers = [(b"content-type", b"application/json"), (b"cache-control", b"no-store")]
        if status == 401:
            headers.append((b"www-authenticate", b"Bearer"))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def _handle(self, scope: dict, receive: Callable) -> tuple[int, bytes]:
        auth = None
        for name, value in scope.get("headers", []):
            if name.lower() == b"authorization":
                if auth is not None:   # two Authorization headers: ambiguous, refuse
                    raise GateError(401, "unauthorized")
                auth = value.decode("latin-1")
        if not bearer_ok(auth, self._token):
            raise GateError(401, "unauthorized")
        method, path = scope.get("method", ""), scope.get("path", "")
        if (method, path) not in ALLOWED:
            raise GateError(404, "not found")
        body = await _read_body(receive, MAX_BODY_BYTES)
        if method == "POST":
            body = prepare_chat(body)
        else:
            body = b""
        return await self._upstream(method, path, body)

    @staticmethod
    async def _lifespan(receive: Callable, send: Callable) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return


async def _read_body(receive: Callable, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        message: dict[str, Any] = await receive()
        if message["type"] == "http.disconnect":
            raise GateError(400, "client disconnected")
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > limit:
            raise GateError(413, "request too large")
        chunks.append(chunk)
        if not message.get("more_body"):
            return b"".join(chunks)
