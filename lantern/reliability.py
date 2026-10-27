"""Staying up when the network or the API wobbles (meta-model-cookbook 01_api_fundamentals/09_error_handling).

  - Retry only what is temporary: 429, 5xx, timeouts and connection errors, with exponential
    backoff plus jitter (and Retry-After when the server sends it), capped. Never retry 400/401/404:
    the request, key or model is wrong and retrying only wastes time.
  - A circuit breaker stops calling after repeated failures, then lets one trial call through.
  - Every failure becomes a plain sentence for the user ("the key is wrong", "no internet").
RetryingClient wraps the *audited* client, so each attempt that leaves the laptop is audited; the
OpenAI SDK's own silent retries are switched off (max_retries=0) so ours are visible as events.
"""
from __future__ import annotations

import random
import time
from typing import Any, Callable

RETRY_STATUSES = {408, 409, 425, 429}
TRANSIENT_NAMES = {"APITimeoutError", "APIConnectionError", "RateLimitError", "InternalServerError",
                   "TimeoutError", "ConnectionError", "ConnectionResetError", "RemoteDisconnected"}


class UserFacingError(RuntimeError):
    """An error whose message is already a sentence for the user (explain() passes it through)."""


class OfflineError(UserFacingError):
    """Lantern can't reach the model API right now. The message is meant for the user."""


def status_of(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    if status is None and getattr(exc, "response", None) is not None:
        status = getattr(exc.response, "status_code", None)
    return status if isinstance(status, int) else None


def is_transient(exc: BaseException) -> bool:
    status = status_of(exc)
    if status is not None:
        return status in RETRY_STATUSES or status >= 500
    return type(exc).__name__ in TRANSIENT_NAMES or isinstance(exc, (TimeoutError, ConnectionError))


def retry_after(exc: BaseException) -> float | None:
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    try:
        value = float(headers.get("retry-after") or headers.get("Retry-After") or "")
    except (TypeError, ValueError):
        return None
    return min(max(value, 0.0), 30.0)


def explain(exc: BaseException) -> str:
    """One sentence a person can act on."""
    if isinstance(exc, UserFacingError):
        return str(exc)
    status = status_of(exc)
    body = getattr(exc, "body", None)
    detail = (body.get("message") if isinstance(body, dict) else None) or str(exc)
    if status == 401:
        return "Meta's API refused the key: check MODEL_API_KEY in .env."
    if status == 403:
        return "This key isn't allowed to use that model or feature."
    if status == 404:
        return "Model or endpoint not found: check the model name in lantern/models.py."
    if status == 400:
        return f"The API rejected the request: {detail[:200]}"
    if status == 429:
        return "Rate limited by Meta's API; Lantern backed off and gave up for now. Try again in a minute."
    if status is not None and status >= 500:
        return f"Meta's API had a server error ({status}); Lantern retried and gave up for now."
    if type(exc).__name__ in ("APITimeoutError", "TimeoutError") or isinstance(exc, TimeoutError):
        return "The API took too long to answer; Lantern retried and gave up for now."
    if type(exc).__name__ in ("APIConnectionError", "ConnectionError") or isinstance(exc, ConnectionError):
        return "Lantern can't reach api.meta.ai: are you online? Private-lane and local tools still work."
    return f"{type(exc).__name__}: {str(exc)[:200]}"


class CircuitBreaker:
    """Open after `threshold` failures in a row; after `cooldown` seconds let one trial call through."""

    def __init__(self, threshold: int = 3, cooldown: float = 30.0, clock: Callable[[], float] = time.monotonic):
        self.threshold, self.cooldown, self.clock = threshold, cooldown, clock
        self.failures = 0
        self.opened_at: float | None = None

    def allow(self) -> bool:
        if self.opened_at is None:
            return True
        if self.clock() - self.opened_at >= self.cooldown:
            self.opened_at = None  # half open: one trial; failures stay at the threshold
            return True
        return False

    def record(self, ok: bool) -> None:
        if ok:
            self.failures, self.opened_at = 0, None
            return
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = self.clock()

    def remaining(self) -> float:
        return 0.0 if self.opened_at is None else max(0.0, self.cooldown - (self.clock() - self.opened_at))


class _Endpoint:
    def __init__(self, owner: "RetryingClient", target: Any):
        self._owner, self._target = owner, target

    def create(self, **kw):
        return self._owner._call(self._target.create, kw)


class _Chat:
    def __init__(self, owner: "RetryingClient", inner: Any):
        self.completions = _Endpoint(owner, inner.completions)


class RetryingClient:
    """Same `.responses.create` / `.chat.completions.create` surface, with retries and a breaker."""

    def __init__(self, client: Any, *, max_retries: int = 4, base_delay: float = 0.5, max_delay: float = 8.0,
                 breaker: CircuitBreaker | None = None, sleep: Callable[[float], None] = time.sleep,
                 on_retry: Callable[[dict], None] | None = None, before_call: Callable[[], None] | None = None):
        self.inner = client
        self.before_call = before_call   # e.g. BudgetGuard.check: raises to refuse; never retried
        self.max_retries = max_retries
        self.base_delay, self.max_delay = base_delay, max_delay
        self.breaker = breaker or CircuitBreaker()
        self.sleep = sleep
        self.on_retry = on_retry or (lambda info: None)
        if hasattr(client, "responses"):
            self.responses = _Endpoint(self, client.responses)
        if hasattr(client, "chat"):
            self.chat = _Chat(self, client.chat)

    def __getattr__(self, name: str) -> Any:  # set_task, audit, ... of the wrapped (audited) client
        return getattr(self.inner, name)

    def _call(self, fn: Callable[..., Any], kw: dict) -> Any:
        if self.before_call is not None:
            self.before_call()
        if not self.breaker.allow():
            raise OfflineError(f"Lantern paused calls to Meta's API after repeated failures; "
                               f"it will try again in {self.breaker.remaining():.0f} s.")
        for attempt in range(self.max_retries + 1):
            try:
                result = fn(**kw)
            except Exception as e:
                if not is_transient(e):
                    raise  # 4xx and programming errors: fail fast, nothing to wait for
                if attempt == self.max_retries:
                    self.breaker.record(ok=False)
                    raise
                wait = retry_after(e)
                if wait is None:
                    wait = min(self.max_delay, self.base_delay * 2 ** attempt) + random.uniform(0, self.base_delay)
                self.on_retry({"attempt": attempt + 1, "wait_s": round(wait, 2), "error": explain(e)})
                self.sleep(wait)
                continue
            self.breaker.record(ok=True)
            return result
        raise RuntimeError("unreachable")
