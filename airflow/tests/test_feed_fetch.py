"""The retry policy every interval-feed request goes through (spec 006 section 7, criterion 7).

No network: the transport is a scripted fake that returns recorded statuses in order, the clock
is a list that records every wait, and the random source is seeded.
"""

from __future__ import annotations

import random

import pytest
import requests
from nordbank_ops.feeds.fetch import FetchFailedError, RetryPolicy, fetch

URL = "https://api.frankfurter.dev/v1/2026-07-24"
POLICY = RetryPolicy(max_attempts=4, base_delay=1.0, max_delay=8.0, timeout=5.0)


class _Response:
    def __init__(self, status: int, body: bytes = b"{}", headers: dict | None = None) -> None:
        self.status_code = status
        self.content = body
        self.headers = headers or {}


def _script(*outcomes):
    """A fake `requests.get` that plays outcomes in order: a status, a response or an error."""
    calls = []
    remaining = list(outcomes)

    def get(url, params=None, timeout=None):
        calls.append({"url": url, "params": params, "timeout": timeout})
        outcome = remaining.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, int):
            return _Response(outcome)
        return outcome

    return get, calls


def _run(*outcomes):
    get, calls = _script(*outcomes)
    waits: list[float] = []
    result = fetch(URL, policy=POLICY, get=get, sleep=waits.append, rng=random.Random(7))
    return result, calls, waits


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(429, id="rate-limit"),
        pytest.param(requests.Timeout("read timed out"), id="timeout"),
        pytest.param(503, id="server-error"),
    ],
)
def test_each_transient_failure_is_retried_and_then_succeeds(failure) -> None:
    result, calls, waits = _run(failure, failure, 200)
    assert result.ok and result.status == 200
    assert result.attempt_count == 3 and len(calls) == 3
    assert len(waits) == 2
    assert all(call["timeout"] == POLICY.timeout for call in calls)


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(429, id="rate-limit"),
        pytest.param(requests.Timeout("read timed out"), id="timeout"),
        pytest.param(500, id="server-error"),
    ],
)
def test_each_transient_failure_gives_up_at_the_attempt_cap(failure) -> None:
    get, calls = _script(*([failure] * POLICY.max_attempts))
    waits: list[float] = []
    with pytest.raises(FetchFailedError) as raised:
        fetch(URL, policy=POLICY, get=get, sleep=waits.append, rng=random.Random(7))
    assert len(calls) == POLICY.max_attempts
    assert raised.value.result.attempt_count == POLICY.max_attempts
    # No wait after the last attempt: the cap bounds the total, not only the count.
    assert len(waits) == POLICY.max_attempts - 1


def test_the_backoff_is_exponential_with_full_jitter_and_capped() -> None:
    rng = random.Random(11)
    ceilings = [min(POLICY.base_delay * 2 ** (n - 1), POLICY.max_delay) for n in range(1, 7)]
    assert ceilings == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0]
    for attempt, ceiling in enumerate(ceilings, start=1):
        for _ in range(50):
            assert 0.0 <= POLICY.delay(attempt, rng) <= ceiling
    draws = {round(POLICY.delay(3, rng), 6) for _ in range(20)}
    assert len(draws) > 1, "jitter produced one value, so retries would move in lockstep"


def test_a_rate_limit_honours_retry_after_up_to_the_cap() -> None:
    result, _calls, waits = _run(
        _Response(429, headers={"Retry-After": "3"}),
        _Response(429, headers={"Retry-After": "3600"}),
        200,
    )
    assert result.ok
    assert waits == [3.0, POLICY.max_delay]


@pytest.mark.parametrize("status", [404, 422, 400])
def test_a_client_error_is_returned_once_and_not_retried(status) -> None:
    result, calls, waits = _run(status)
    assert result.status == status and not result.ok
    assert len(calls) == 1 and waits == []


def test_an_error_quoting_the_request_url_has_its_key_redacted() -> None:
    leak = requests.ConnectionError(
        "Max retries exceeded with url: /fred/series/observations?series_id=UNRATE"
        "&api_key=abc123secret&file_type=json"
    )
    get, _calls = _script(*([leak] * POLICY.max_attempts))
    with pytest.raises(FetchFailedError) as raised:
        fetch(URL, policy=POLICY, get=get, sleep=lambda _s: None, rng=random.Random(1))
    text = str(raised.value) + " ".join(a.error or "" for a in raised.value.result.attempts)
    assert "abc123secret" not in text
    assert "api_key=REDACTED" in text
