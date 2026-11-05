"""A local stand-in for the Meta Model API, spoken over real HTTP, so tests can drive Lantern through the
real OpenAI SDK (serialization, response objects, auth header, HTTP errors and retries) without ever
calling the real API. Never used outside tests.

    with FakeMetaAPI([reply_text("hi")]) as api:
        client = OpenAI(base_url=api.url, api_key="test-key", max_retries=0)

Script items, used in order for POST /v1/responses:
  dict            a Responses API JSON body (build with reply_text / reply_calls / reply_computer)
  (status, body)  an HTTP error, e.g. (429, {"error": ...}); body may carry "_headers"
POST /v1/asr/transcribe answers with `transcript` (multipart body recorded as bytes).
"""
from __future__ import annotations

import itertools
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_ids = itertools.count(1)


def _usage(inp=120, out=20, cached=0):
    return {"input_tokens": inp, "input_tokens_details": {"cached_tokens": cached}, "output_tokens": out,
            "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": inp + out}


def _response(output: list, model="muse-spark-1.3", usage=None) -> dict:
    return {"id": f"resp_{next(_ids)}", "object": "response", "created_at": 1793000000, "model": model,
            "status": "completed", "output": output, "parallel_tool_calls": True, "tool_choice": "auto",
            "tools": [], "usage": usage or _usage()}


def reply_text(text: str, **kw) -> dict:
    return _response([{"type": "message", "id": f"msg_{next(_ids)}", "role": "assistant", "status": "completed",
                       "content": [{"type": "output_text", "text": text, "annotations": []}]}], **kw)


def reply_calls(*calls: tuple[str, dict], **kw) -> dict:
    return _response([{"type": "function_call", "id": f"fc_{next(_ids)}", "call_id": f"call_{next(_ids)}",
                       "name": name, "arguments": json.dumps(args), "status": "completed"} for name, args in calls], **kw)


def reply_computer(*actions: dict, checks=None, **kw) -> dict:
    """Meta's native computer tool: one computer_call with an ordered `actions` list."""
    return _response([{"type": "computer_call", "id": f"cu_{next(_ids)}", "call_id": f"call_{next(_ids)}",
                       "actions": list(actions), "pending_safety_checks": list(checks or []), "status": "completed"}], **kw)


def stream_text(text: str, model: str = "sam-3.1", status: str = "completed") -> tuple:
    """A streamed response whose single output_text is `text` (SAM 3.1 streams its records this way)."""
    lane = {"item_id": "msg_s1", "output_index": 0, "content_index": 0}
    half = len(text) // 2
    done = _response([{"type": "message", "id": "msg_s1", "role": "assistant", "status": "completed",
                       "content": [{"type": "output_text", "text": text, "annotations": []}]}], model=model)
    done["status"] = status
    return ("stream", [
        {"type": "response.created", "response": {**done, "status": "in_progress", "output": []}},
        {"type": "response.output_text.delta", **lane, "delta": text[:half], "logprobs": []},
        {"type": "response.output_text.delta", **lane, "delta": text[half:], "logprobs": []},
        {"type": "response.content_part.done", **lane, "part": {"type": "output_text", "text": text, "annotations": []}},
        {"type": f"response.{status}", "response": done},
    ])


class FakeMetaAPI:
    def __init__(self, script, transcript: str = "fill the reimbursement form"):
        self.script = list(script)
        self.transcript = transcript
        self.requests: list[dict] = []     # {path, headers, json | raw}
        self._lock = threading.Lock()
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):   # keep test output clean
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                entry = {"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "raw": raw}
                if self.path.endswith("/asr/transcribe"):
                    api._record(entry)
                    return self._send(200, {"transcript": api.transcript, "audioDurationMs": 1500})
                try:
                    entry["json"] = json.loads(raw or b"{}")
                except ValueError:
                    return self._send(400, {"error": {"message": "body is not JSON", "type": "invalid_request_error"}})
                api._record(entry)
                with api._lock:
                    item = api.script.pop(0) if api.script else (500, {"error": {"message": "script exhausted"}})
                if isinstance(item, tuple) and item[0] == "stream":
                    return self._stream(item[1])
                if isinstance(item, tuple):
                    status, body = item
                    return self._send(status, {k: v for k, v in body.items() if k != "_headers"}, body.get("_headers"))
                return self._send(200, item)

            def _stream(self, events):
                """Server-sent events, the way the Responses API streams (stream=True)."""
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                for n, event in enumerate(events):
                    data = json.dumps({"sequence_number": n, **event})
                    self.wfile.write(f"event: {event['type']}\ndata: {data}\n\n".encode())
                    self.wfile.flush()
                self.close_connection = True

            def _send(self, status, body, headers=None):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, str(v))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def _record(self, entry):
        with self._lock:
            self.requests.append(entry)

    def bodies(self, path: str = "/v1/responses") -> list[dict]:
        return [r["json"] for r in self.requests if r["path"] == path and "json" in r]

    def __enter__(self) -> "FakeMetaAPI":
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()
