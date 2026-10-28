"""deploy/modal/gate.py: the bearer-token gate, driven as a plain ASGI app with a fake upstream."""
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "deploy" / "modal"))
import gate as g  # noqa: E402
from glimmer_config import ConfigError  # noqa: E402

TOKEN = "t" * 20 + "_-" + "K" * 21   # 43 chars, like token_urlsafe(32)


class FakeUpstream:
    def __init__(self, status=200, body=b'{"ok": true}', fail=False):
        self.calls = []
        self.status, self.body, self.fail = status, body, fail

    async def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        if self.fail:
            raise ConnectionError("gpu gone, prompt was: secret stuff")
        return self.status, self.body


def call(app, method="POST", path="/v1/chat/completions", headers=None, body=b"", chunks=None):
    """Run one ASGI request; returns (status, headers dict, parsed JSON body)."""
    scope = {"type": "http", "method": method, "path": path,
             "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]}
    parts = chunks if chunks is not None else [body]
    messages = [{"type": "http.request", "body": p, "more_body": i < len(parts) - 1} for i, p in enumerate(parts)]
    sent = []

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(msg):
        sent.append(msg)

    asyncio.run(app(scope, receive, send))
    start, out = sent[0], sent[1]
    return start["status"], {k.decode(): v.decode() for k, v in start["headers"]}, json.loads(out["body"])


def auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


def chat_body(**extra):
    return json.dumps({"model": "x", "messages": [{"role": "user", "content": "hi"}], **extra}).encode()


def test_refuses_to_start_without_a_strong_token():
    for bad in (None, "", "short"):
        with pytest.raises(ConfigError):
            g.Gate(bad, FakeUpstream())


@pytest.mark.parametrize("headers", [
    {}, {"Authorization": ""}, {"Authorization": "Bearer"}, {"Authorization": "Bearer "},
    {"Authorization": f"Basic {TOKEN}"}, {"Authorization": TOKEN},
    {"Authorization": f"Bearer {TOKEN}x"}, {"Authorization": f"Bearer {TOKEN[:-1]}"},
    {"Authorization": "Bearer " + "u" * 43}, {"X-Api-Key": TOKEN},
])
def test_rejects_everything_without_the_exact_token(headers):
    up = FakeUpstream()
    status, hdrs, body = call(g.Gate(TOKEN, up), headers=headers, body=chat_body())
    assert status == 401 and hdrs["www-authenticate"] == "Bearer"
    assert body["error"]["message"] == "unauthorized"
    assert up.calls == []                        # the GPU is never called, so never woken


def test_auth_is_checked_before_the_path():
    up = FakeUpstream()
    assert call(g.Gate(TOKEN, up), method="GET", path="/slots")[0] == 401
    assert call(g.Gate(TOKEN, up), method="GET", path="/slots", headers=auth())[0] == 404
    assert up.calls == []


def test_two_authorization_headers_are_refused():
    up = FakeUpstream()
    app = g.Gate(TOKEN, up)
    scope_headers = [(b"authorization", f"Bearer {TOKEN}".encode()), (b"authorization", b"Bearer other")]

    async def run():
        sent = []
        msgs = [{"type": "http.request", "body": chat_body()}]
        await app({"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": scope_headers},
                  lambda: asyncio.sleep(0, msgs.pop(0)), lambda m: asyncio.sleep(0, sent.append(m)))
        return sent
    assert asyncio.run(run())[0]["status"] == 401 and up.calls == []


def test_scheme_is_case_insensitive():
    up = FakeUpstream()
    assert call(g.Gate(TOKEN, up), headers={"Authorization": f"bearer {TOKEN}"}, body=chat_body())[0] == 200


@pytest.mark.parametrize("method,path", [
    ("GET", "/"), ("GET", "/health"), ("GET", "/slots"), ("GET", "/props"), ("GET", "/metrics"),
    ("POST", "/completion"), ("POST", "/v1/completions"), ("POST", "/v1/embeddings"),
    ("POST", "/v1/chat/completions/"), ("GET", "/v1/chat/completions"), ("POST", "/v1/models"),
    ("POST", "/slots/0?action=save"), ("DELETE", "/v1/models"),
])
def test_only_two_routes_exist(method, path):
    up = FakeUpstream()
    status, _, _ = call(g.Gate(TOKEN, up), method=method, path=path, headers=auth(), body=chat_body())
    assert status == 404 and up.calls == []


def test_models_forwards_without_a_body():
    up = FakeUpstream(body=b'{"data": [{"id": "muse-glimmer"}]}')
    status, _, body = call(g.Gate(TOKEN, up), method="GET", path="/v1/models", headers=auth(), body=b"junk")
    assert status == 200 and body["data"][0]["id"] == "muse-glimmer"
    assert up.calls == [("GET", "/v1/models", b"")]


def test_chat_gets_glimmer_sampling_and_one_tool_call_per_turn():
    up = FakeUpstream()
    status, hdrs, _ = call(g.Gate(TOKEN, up), headers=auth(), body=chat_body(temperature=0, tools=[]))
    assert status == 200 and hdrs["cache-control"] == "no-store"
    sent = json.loads(up.calls[0][2])
    assert sent["model"] == "muse-glimmer"
    assert (sent["temperature"], sent["top_p"], sent["top_k"]) == (1.0, 0.95, 64)   # 0 raised to 1.0
    assert sent["parallel_tool_calls"] is False
    assert sent["max_tokens"] == 1024


def test_chat_keeps_valid_caller_sampling_and_caps_max_tokens():
    up = FakeUpstream()
    call(g.Gate(TOKEN, up), headers=auth(), body=chat_body(temperature=0.7, top_p=0.9, top_k=40,
                                                          max_completion_tokens=10**6,
                                                          chat_template_kwargs={"reasoning_strength": "low"}))
    sent = json.loads(up.calls[0][2])
    assert (sent["temperature"], sent["top_p"], sent["top_k"]) == (0.7, 0.9, 40)
    assert sent["max_tokens"] == g.MAX_TOKENS_CAP and "max_completion_tokens" not in sent
    assert sent["chat_template_kwargs"] == {"reasoning_strength": "low"}


@pytest.mark.parametrize("body,status", [
    (b"not json", 400), (b"[]", 400), (json.dumps({"messages": []}).encode(), 400),
    (chat_body(stream=True), 400), (chat_body(n=2), 400), (chat_body(max_tokens=-5), 400),
    (chat_body(max_tokens="lots"), 400), (chat_body(temperature=True), 200),
])
def test_bad_chat_bodies(body, status):
    up = FakeUpstream()
    assert call(g.Gate(TOKEN, up), headers=auth(), body=body)[0] == status
    assert len(up.calls) == (1 if status == 200 else 0)


def test_large_bodies_are_refused_even_when_chunked():
    up = FakeUpstream()
    chunk = b"x" * (g.MAX_BODY_BYTES // 4 + 1)
    status, _, _ = call(g.Gate(TOKEN, up), headers=auth(), chunks=[chunk] * 4)
    assert status == 413 and up.calls == []


def test_chunked_body_is_reassembled():
    up = FakeUpstream()
    body = chat_body()
    assert call(g.Gate(TOKEN, up), headers=auth(), chunks=[body[:10], body[10:]])[0] == 200


def test_upstream_status_and_errors_pass_through_without_details():
    status, _, body = call(g.Gate(TOKEN, FakeUpstream(status=400, body=b'{"error": "ctx"}')),
                           headers=auth(), body=chat_body())
    assert status == 400 and body == {"error": "ctx"}
    status, _, body = call(g.Gate(TOKEN, FakeUpstream(fail=True)), headers=auth(), body=chat_body())
    assert status == 502 and "secret" not in json.dumps(body)


def test_gate_prints_nothing(capsys):
    up = FakeUpstream()
    call(g.Gate(TOKEN, up), headers=auth(), body=chat_body(messages=[{"role": "user", "content": "canary42"}]))
    call(g.Gate(TOKEN, FakeUpstream(fail=True)), headers=auth(), body=chat_body())
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_lifespan_and_websocket():
    app = g.Gate(TOKEN, FakeUpstream())
    sent = []
    msgs = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]

    async def run(scope):
        async def receive():
            return msgs.pop(0)

        async def send(m):
            sent.append(m)
        await app(scope, receive, send)
    asyncio.run(run({"type": "lifespan"}))
    assert [m["type"] for m in sent] == ["lifespan.startup.complete", "lifespan.shutdown.complete"]
    sent.clear()
    asyncio.run(run({"type": "websocket", "path": "/v1/models", "headers": []}))
    assert sent == [{"type": "websocket.close", "code": 1008}]


@pytest.mark.parametrize("header,ok", [
    (f"Bearer {TOKEN}", True), (f"BEARER {TOKEN}", True), (f"Bearer  {TOKEN}", True),
    (None, False), ("", False), (f"Token {TOKEN}", False), (f"Bearer {TOKEN} extra", False),
])
def test_bearer_ok(header, ok):
    assert g.bearer_ok(header, TOKEN) is ok
    assert g.bearer_ok(header, "") is False
