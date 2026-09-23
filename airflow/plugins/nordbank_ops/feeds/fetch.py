"""One HTTP request, retried within stated bounds (spec 006 section 7).

Every interval-feed request goes through `fetch`. What it retries, and what it does not:

- **Retried:** a timeout, a connection failure, HTTP 429, and any 5xx. A rate limit is a request
  to come back later, not a refusal, so it is retried rather than failed; a `Retry-After`
  header is honoured, capped at the policy's longest delay so a publisher cannot park a task
  for an hour.
- **Not retried:** any other 4xx. A 404 or a 422 will say the same thing on the next attempt,
  and the caller decides what it means — for the FX feed a 404 is a date beyond all published
  data, which is an absence rather than an error.

The delay before retry `n` is exponential with full jitter: uniform between zero and
`base_delay * 2**(n-1)`, capped at `max_delay`. Jitter matters because several mapped tasks
failing together against one publisher would otherwise retry in lockstep and hit its rate limit
together again. The attempt count is capped, and so is each request's duration, so the worst
case of a request is bounded and stated rather than open-ended.

The clock, the random source and the transport are injectable, so the policy is tested against
recorded responses with no network and no waiting.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

RETRIED_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 5
    base_delay: float = 1.0
    max_delay: float = 30.0
    timeout: float = 20.0

    def delay(self, attempt: int, rng: random.Random, retry_after: float | None = None) -> float:
        """Seconds to wait after failed attempt `attempt`, counting from 1."""
        if retry_after is not None:
            return min(max(retry_after, 0.0), self.max_delay)
        ceiling = min(self.base_delay * 2 ** (attempt - 1), self.max_delay)
        return rng.uniform(0.0, ceiling)


@dataclass
class Attempt:
    status: int | None
    error: str | None
    waited: float


@dataclass
class FetchResult:
    """The last attempt's outcome and the history that led to it."""

    url: str
    status: int | None
    body: bytes | None
    attempts: list[Attempt] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)


class FetchFailedError(RuntimeError):
    """Every attempt was used and the last one was still retryable."""

    def __init__(self, result: FetchResult) -> None:
        self.result = result
        last = result.attempts[-1] if result.attempts else None
        reason = (last.error or f"HTTP {last.status}") if last else "no attempt made"
        super().__init__(f"{result.url}: gave up after {result.attempt_count} attempt(s): {reason}")


def _retry_after(headers: Any) -> float | None:
    raw = headers.get("Retry-After") if headers is not None else None
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def fetch(
    url: str,
    *,
    params: dict | None = None,
    policy: RetryPolicy | None = None,
    get: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> FetchResult:
    """GET a URL under the policy. Raises `FetchFailedError` when retries are exhausted.

    A non-retried status is returned rather than raised, so the caller can decide whether a
    404 is an absence or an error.
    """
    import requests

    policy = policy or RetryPolicy()
    get = get or requests.get
    rng = rng or random.Random()
    result = FetchResult(url=url, status=None, body=None)

    for attempt in range(1, policy.max_attempts + 1):
        retry_after = None
        try:
            response = get(url, params=params, timeout=policy.timeout)
        except (requests.Timeout, requests.ConnectionError) as exc:
            result.status, result.body = None, None
            result.error = f"{type(exc).__name__}: {exc}"
            result.attempts.append(Attempt(status=None, error=result.error, waited=0.0))
        else:
            result.status, result.body, result.error = response.status_code, response.content, None
            result.attempts.append(Attempt(status=response.status_code, error=None, waited=0.0))
            if response.status_code not in RETRIED_STATUSES:
                return result
            retry_after = _retry_after(getattr(response, "headers", None))

        if attempt == policy.max_attempts:
            break
        wait = policy.delay(attempt, rng, retry_after)
        result.attempts[-1].waited = wait
        sleep(wait)

    raise FetchFailedError(result)
