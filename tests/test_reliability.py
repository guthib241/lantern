"""M17: retries for temporary failures only, a circuit breaker, plain-language errors, Try again."""
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from fakes import FakeClient, usage
from lantern.config import Config
from lantern.core import Core
from lantern.reliability import (CircuitBreaker, OfflineError, RetryingClient, explain, is_transient,
                                 retry_after)
from lantern.server import create_app


class APIError(Exception):
    """Shaped like the OpenAI SDK's APIStatusError: status_code, response.headers, body."""

    def __init__(self, status, headers=None, message="boom"):
        super().__init__(message)
        self.status_code = status
        self.response = NS(status_code=status, headers=headers or {})
        self.body = {"message": message}


class APITimeoutError(Exception):
    pass


class APIConnectionError(Exception):
    pass


class Flaky:
    """`.responses.create` raises the given errors in order, then returns 'ok'."""

    def __init__(self, *errors):
        self.errors = list(errors)
        self.calls = 0
        self.responses = self
        self.task = None

    def create(self, **kw):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return "ok"

    def set_task(self, task_id):
        self.task = task_id


def wrap(inner, **kw):
    slept, retries = [], []
    client = RetryingClient(inner, sleep=slept.append, on_retry=retries.append, **kw)
    return client, slept, retries


@pytest.mark.parametrize("err", [APIError(429), APIError(500), APIError(503), APIError(408),
                                 APITimeoutError("slow"), APIConnectionError("down"), TimeoutError()])
def test_temporary_failures_are_retried(err):
    inner = Flaky(err, err)
    client, slept, retries = wrap(inner)
    assert client.responses.create(model="m") == "ok"
    assert inner.calls == 3
    assert len(slept) == 2 and slept[0] < slept[1] <= 8.5  # exponential backoff (plus jitter), capped
    assert [r["attempt"] for r in retries] == [1, 2]
    assert all(r["error"] for r in retries)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_request_errors_fail_fast(status):
    inner = Flaky(APIError(status))
    client, slept, _ = wrap(inner)
    with pytest.raises(APIError):
        client.responses.create(model="m")
    assert inner.calls == 1 and slept == []
    assert client.breaker.failures == 0  # a bad request says nothing about the network


def test_retry_after_header_is_respected_and_capped():
    assert retry_after(APIError(429, {"retry-after": "3"})) == 3.0
    assert retry_after(APIError(429, {"Retry-After": "600"})) == 30.0
    assert retry_after(APIError(429)) is None
    inner = Flaky(APIError(429, {"retry-after": "2.5"}))
    client, slept, _ = wrap(inner)
    client.responses.create(model="m")
    assert slept == [2.5]


def test_gives_up_after_max_retries_and_breaker_opens():
    now = [0.0]
    breaker = CircuitBreaker(threshold=2, cooldown=30, clock=lambda: now[0])
    inner = Flaky(*[APIError(503)] * 20)
    client, slept, _ = wrap(inner, max_retries=2, breaker=breaker)
    for _ in range(2):
        with pytest.raises(APIError):
            client.responses.create(model="m")
    assert inner.calls == 6
    with pytest.raises(OfflineError) as e:  # open: no call leaves the laptop
        client.responses.create(model="m")
    assert inner.calls == 6 and "30 s" in str(e.value)
    now[0] = 31.0  # half open: one trial goes through and succeeds
    inner.errors = []
    assert client.responses.create(model="m") == "ok"
    assert breaker.failures == 0 and breaker.allow()


def test_wrapper_forwards_the_audited_client_surface():
    inner = Flaky()
    client, _, _ = wrap(inner)
    client.set_task(42)
    assert inner.task == 42
    chat = NS(completions=NS(create=lambda **kw: kw["model"]))
    assert RetryingClient(NS(chat=chat)).chat.completions.create(model="glimmer") == "glimmer"


def test_explain_gives_a_sentence_a_person_can_act_on():
    assert "MODEL_API_KEY" in explain(APIError(401))
    assert "lantern/models.py" in explain(APIError(404))
    assert "Rate limited" in explain(APIError(429))
    assert "server error (502)" in explain(APIError(502))
    assert "bad field" in explain(APIError(400, message="bad field"))
    assert "online" in explain(APIConnectionError("x"))
    assert "too long" in explain(APITimeoutError("x"))
    assert explain(OfflineError("paused")) == "paused"
    assert explain(ValueError("odd")) == "ValueError: odd"
    assert not is_transient(ValueError("odd"))


def make_core(tmp_path, client):
    root = tmp_path / "docs"
    root.mkdir(exist_ok=True)
    cfg = Config(roots=[str(root)], db_path=str(tmp_path / "m.db"))
    cfg.audit_path = str(tmp_path / "audit.db")
    cfg.zones_file = str(tmp_path / "zones.json")
    core = Core(cfg, client, approval_timeout=5)
    core.client.sleep = lambda s: None  # no real waiting in tests
    return core


class Script(FakeClient):
    """FakeClient whose script may also contain exceptions to raise."""

    def create(self, **kw):
        if self.script and isinstance(self.script[0], Exception):
            self.requests.append(kw)
            raise self.script.pop(0)
        return super().create(**kw)


def test_core_retries_visibly_and_every_attempt_is_audited(tmp_path):
    core = make_core(tmp_path, Script([APIError(503), ([], "Hello.", usage(10, 0, 2))]))
    events = []
    core.subscribe(events.append)
    tid = core.submit("hi")
    assert core.wait_idle()
    assert core.memory.get_task(tid).status == "done"
    retry = [e for e in events if e["type"] == "retry"]
    assert len(retry) == 1 and "server error" in retry[0]["error"]
    assert core.audit.summary()["cloud"]["calls"] == 2


def test_failed_task_says_why_and_can_be_tried_again(tmp_path):
    core = make_core(tmp_path, Script([APIError(401), ([], "Hello again.", usage(10, 0, 2))]))
    events = []
    core.subscribe(events.append)
    c = TestClient(create_app(core, "tok"))
    h = {"X-Lantern-Token": "tok"}
    tid = c.post("/task", json={"goal": "hi"}, headers=h).json()["task_id"]
    assert core.wait_idle()
    done = [e for e in events if e["type"] == "done"][-1]
    assert done["status"] == "failed" and "MODEL_API_KEY" in done["reply"]
    assert "MODEL_API_KEY" in core.memory.get_task(tid).summary
    again = c.post(f"/tasks/{tid}/retry", headers=h).json()["task_id"]
    assert again != tid
    assert core.wait_idle()
    t = core.memory.get_task(again)
    assert t.status == "done" and t.goal == "hi"
    assert c.post("/tasks/999/retry", headers=h).status_code == 404
