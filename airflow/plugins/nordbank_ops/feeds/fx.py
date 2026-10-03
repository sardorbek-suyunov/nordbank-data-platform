"""The ECB reference rates, one request per date (spec 006 section 2).

**The landing rule is the whole control.** Asked for a date the ECB did not publish — a weekend,
a TARGET holiday, a date after the latest publication — Frankfurter v1 does not refuse: it
answers HTTP 200 with the previous publication's rates and that publication's date. Measured on
2026-09-22 for Saturday 2026-07-25, which came back dated 2026-07-24, and for 2026-09-23, the
day after the measurement, which came back dated 2026-09-22. Only a date beyond all data
returns 404. So a response is landed only when its `date` equals the date requested; otherwise
nothing is written for the date, which is how bronze records a gap: as an absence, with the
request logged as `absent_no_publication` in `ops.feed_request`, with the date the API
answered with. That status is distinct from `landed` (rows written) and `discarded` (answered
for itself, inside an interval that failed and wrote nothing), and the count of unpublished
dates is read from it. Carrying the last rate forward is silver's
job, and doing it here would destroy the evidence that the gap existed.

The same rule makes a rate for a simulated day ahead of the real clock impossible to fabricate:
the API answers such a request with an earlier date, and the rule lands nothing.

Rates are parsed from the JSON as decimals and never pass through a float.

**History arrives through the time-series endpoint, once.** The daily run requests its own date
only, so before this the platform held no rate before its first day, and 64 per cent of the `ci`
book's non-EUR transactions had none to convert at. The history before the first day is one
request, `GET /v1/<from>..<to>`, measured on 2026-10-02 before relying on it: it answers with
`start_date`, `end_date` and `rates` keyed by date; every returned date is a publication (634
dates for 2024-01-23 to 2026-07-19, no weekend, no sampling, gaps only at weekends and TARGET
holidays); its rates for 2026-07-20 equal the single-date response's; and a range starting or
ending on a day the ECB did not publish **snaps back** to the publication before it: asked from
Saturday 2026-07-18, it started at Friday 2026-07-17. Each returned date lands as the daily
request would have landed it, and a date not returned is absent, never filled. The one date
before the requested start is that snapped-back publication, and it is landed, because it is the
rate in force at the start; any other date outside the request refuses the whole response.
"""

from __future__ import annotations

import datetime as dt
import decimal
import json
from dataclasses import dataclass, field

from nordbank_ops.feeds.land import Parsed

LANDED = "landed"
ABSENT = "absent_no_publication"
FAILED = "failed"
DISCARDED = "discarded"


@dataclass
class RequestOutcome:
    """What one request came to: the date or series it asked for, and how it ended."""

    key: str
    outcome: str
    status: int | None
    attempts: int
    rows: int = 0
    detail: str | None = None

    def as_dict(self) -> dict:
        return {
            "request_key": self.key,
            "outcome": self.outcome,
            "final_status": self.status,
            "attempts": self.attempts,
            "rows_landed": self.rows,
            "detail": self.detail,
        }


@dataclass
class Fetched:
    parsed: Parsed = field(default_factory=Parsed)
    outcomes: list[RequestOutcome] = field(default_factory=list)
    failure: str | None = None


def _answered_date(body: bytes) -> str:
    """The publication date a response is for, as the API stated it, for the request log."""
    try:
        return str(json.loads(body).get("date") or "no date")
    except (ValueError, AttributeError):
        return "an unreadable body"


def dates_to_fetch(watermark: dt.date | None, day: dt.date) -> list[dt.date]:
    """Every date after the last one requested, through the run's own date.

    Normally that is one date. After a missed run it is the gap as well, one request each, so a
    catch-up is not a separate mechanism. The first run of a feed requests its own date only:
    history before the platform existed is a backfill decision, not something a run infers.
    """
    if watermark is None or watermark >= day:
        return [day]
    return [watermark + dt.timedelta(days=n) for n in range(1, (day - watermark).days + 1)]


def shape_drift(response: dict, contract) -> tuple[list[dict], str | None]:
    return _shape_drift(response, contract.format["top_level"])


def _shape_drift(response: dict, expected: dict) -> tuple[list[dict], str | None]:
    observations = []
    for key in response:
        if key not in expected:
            observations.append(
                {
                    "column": key,
                    "kind": "additive",
                    "detail": "a response key the contract does not describe",
                    "action": "logged, not landed",
                }
            )
    missing = [key for key in expected if key not in response]
    for key in missing:
        observations.append(
            {
                "column": key,
                "kind": "removed_column",
                "detail": "a response key the contract requires is absent",
                "action": "batch failed",
            }
        )
    wrong = [
        key
        for key, kind in expected.items()
        if key in response and not _is_kind(response[key], kind)
    ]
    for key in wrong:
        observations.append(
            {
                "column": key,
                "kind": "type_change",
                "detail": f"expected {expected[key]}, got {type(response[key]).__name__}",
                "action": "batch failed",
            }
        )
    if missing or wrong:
        return observations, f"response shape changed: missing {missing}, retyped {wrong}"
    return observations, None


def _is_kind(value, kind: str) -> bool:
    if kind == "number":
        return isinstance(value, decimal.Decimal | int) and not isinstance(value, bool)
    if kind in ("string", "date"):
        return isinstance(value, str)
    if kind == "object":
        return isinstance(value, dict)
    if kind == "array":
        return isinstance(value, list)
    return False


def parse_response(
    body: bytes, requested: dt.date, contract
) -> tuple[list, list, list, str | None]:
    """Records, payloads, drift and a breaking reason for one date's response.

    Returns no records when the response is for another date, which is the absence rule.
    """
    response = json.loads(body.decode("utf-8"), parse_float=decimal.Decimal)
    if not isinstance(response, dict):
        # Frankfurter v2 answers with a list of per-currency rows. Reaching v1's endpoint and
        # getting that back is the shape change the contract's review trigger exists for.
        observation = {
            "column": "*",
            "kind": "type_change",
            "detail": f"the response is a JSON {type(response).__name__}, not an object",
            "action": "batch failed",
        }
        return [], [], [observation], "response shape changed: not a JSON object"
    drift, breaking = shape_drift(response, contract)
    if breaking:
        return [], [], drift, breaking
    served = dt.date.fromisoformat(response["date"])
    if served != requested:
        return [], [], drift, None
    payload = body.decode("utf-8")
    records = []
    for currency in sorted(response["rates"]):
        records.append(
            {
                "rate_date": served,
                "base_currency": response["base"],
                "quote_currency": currency,
                "rate": response["rates"][currency],
            }
        )
    return records, [payload] * len(records), drift, None


QUERY = {"base": "EUR"}


def request_url(base_url: str, requested: dt.date) -> str:
    """The endpoint for one date, without its query."""
    return f"{base_url.rstrip('/')}/{requested.isoformat()}"


def requested_as(base_url: str, requested: dt.date) -> str:
    """The URL one date was requested at, query included: a landed rate's `_source_file`."""
    query = "&".join(f"{key}={value}" for key, value in QUERY.items())
    return f"{request_url(base_url, requested)}?{query}"


def fetch_dates(dates: list[dt.date], contract, *, base_url: str, fetch) -> Fetched:
    """Request every date, and refuse the whole interval if any request fails.

    Nothing is written until every request has an answer, so a partially fetched interval
    lands nothing and its batch is failed with the dates that did not answer; the watermark
    stays where it was and the next run requests the whole interval again.
    """
    from nordbank_ops.feeds.fetch import FetchFailedError

    out = Fetched()
    for requested in dates:
        url = request_url(base_url, requested)
        try:
            result = fetch(url, params=QUERY)
        except FetchFailedError as exc:
            last = exc.result.attempts[-1] if exc.result.attempts else None
            out.outcomes.append(
                RequestOutcome(
                    requested.isoformat(),
                    FAILED,
                    last.status if last else None,
                    exc.result.attempt_count,
                    detail=str(exc),
                )
            )
            continue
        if result.status == 404:
            out.outcomes.append(
                RequestOutcome(
                    requested.isoformat(),
                    ABSENT,
                    404,
                    result.attempt_count,
                    detail="beyond published data",
                )
            )
            continue
        if not result.ok:
            out.outcomes.append(
                RequestOutcome(
                    requested.isoformat(),
                    FAILED,
                    result.status,
                    result.attempt_count,
                    detail=f"HTTP {result.status}",
                )
            )
            continue
        records, payloads, drift, breaking = parse_response(result.body, requested, contract)
        out.parsed.drift.extend(drift)
        if breaking:
            out.parsed.breaking = breaking
        outcome = LANDED if records else ABSENT
        detail = None if records else f"answered with {_answered_date(result.body)}; nothing landed"
        out.outcomes.append(
            RequestOutcome(
                requested.isoformat(),
                outcome,
                result.status,
                result.attempt_count,
                len(records),
                detail,
            )
        )
        out.parsed.records.extend(records)
        out.parsed.payloads.extend(payloads)

    failed = [o for o in out.outcomes if o.outcome == FAILED]
    if failed:
        answered = len(out.outcomes) - len(failed)
        out.failure = f"{answered} of {len(out.outcomes)} date(s) answered; " + "; ".join(
            f"{o.key}: {o.detail}" for o in failed
        )
        out.parsed.records, out.parsed.payloads = [], []
        for outcome in out.outcomes:
            if outcome.outcome == LANDED:
                outcome.outcome, outcome.rows = DISCARDED, 0
                outcome.detail = "answered; not landed, because the interval failed"
    return out


# --- the history, through the time-series endpoint -------------------------------------------

# The top level of a range response. The contract's `format` describes the per-date response,
# which is what each returned date becomes; the range is a transport for many of them, so its
# own shape is checked here.
RANGE_TOP_LEVEL = {
    "amount": "number",
    "base": "string",
    "start_date": "date",
    "end_date": "date",
    "rates": "object",
}


def range_url(base_url: str, start: dt.date, end: dt.date) -> str:
    """The time-series endpoint for a span of dates, without its query."""
    return f"{base_url.rstrip('/')}/{start.isoformat()}..{end.isoformat()}"


def range_requested_as(base_url: str, start: dt.date, end: dt.date) -> str:
    """The URL the history was requested at, query included: the `_source_file` of its rates."""
    query = "&".join(f"{key}={value}" for key, value in QUERY.items())
    return f"{range_url(base_url, start, end)}?{query}"


def _json_value(value) -> str:
    """A parsed value written back as it was received: a decimal keeps its own digits."""
    if isinstance(value, decimal.Decimal):
        return str(value)
    return json.dumps(value)


def date_payload(response: dict, day: str) -> str:
    """One returned date's slice of the response: the envelope, and that date's rates only.

    The whole body on every record would be the 258 KB response eighteen thousand times over.
    The slice keeps every key in the order received and every number in the digits received.
    """
    parts = []
    for key, value in response.items():
        if key == "rates":
            entries = ",".join(
                f"{json.dumps(currency)}:{_json_value(rate)}"
                for currency, rate in value[day].items()
            )
            parts.append(f'"rates":{{{json.dumps(day)}:{{{entries}}}}}')
        else:
            parts.append(f"{json.dumps(key)}:{_json_value(value)}")
    return "{" + ",".join(parts) + "}"


def parse_range(body: bytes, start: dt.date, end: dt.date) -> tuple[list, list, list, str | None]:
    """Records, payloads, drift and a breaking reason for one time-series response."""
    response = json.loads(body.decode("utf-8"), parse_float=decimal.Decimal)
    if not isinstance(response, dict):
        observation = {
            "column": "*",
            "kind": "type_change",
            "detail": f"the response is a JSON {type(response).__name__}, not an object",
            "action": "batch failed",
        }
        return [], [], [observation], "response shape changed: not a JSON object"
    drift, breaking = _shape_drift(response, RANGE_TOP_LEVEL)
    if breaking:
        return [], [], drift, breaking
    days = sorted(response["rates"])
    try:
        served = [dt.date.fromisoformat(day) for day in days]
    except ValueError:
        return [], [], drift, "response shape changed: a rates key is not a date"
    before = [day for day in served if day < start]
    after = [day for day in served if day > end]
    if after or len(before) > 1 or (before and start in served):
        reason = (
            f"answered with dates outside {start}..{end}: "
            f"{len(before)} before it, {len(after)} after it"
        )
        return [], [], drift, reason
    records, payloads = [], []
    for day, served_day in zip(days, served, strict=True):
        rates = response["rates"][day]
        if not isinstance(rates, dict) or not all(
            _is_kind(rate, "number") for rate in rates.values()
        ):
            return [], [], drift, f"response shape changed: the rates for {day} are not numbers"
        payload = date_payload(response, day)
        for currency in sorted(rates):
            records.append(
                {
                    "rate_date": served_day,
                    "base_currency": response["base"],
                    "quote_currency": currency,
                    "rate": rates[currency],
                }
            )
            payloads.append(payload)
    return records, payloads, drift, None


# A single time-series request was measured complete over five years on 2026-10-02: 2021-07-19 to
# 2026-07-19 returned 1,281 publication dates in 530 KB, the same dates with the same rates as six
# calendar-year requests over the span, with no weekend and no sampling. A longer range is
# unmeasured, so a history longer than that is requested one calendar year at a time.
MEASURED_RANGE_DAYS = 1827


def history_requests(start: dt.date, end: dt.date) -> list[tuple[dt.date, dt.date]]:
    """The spans to request: the whole history up to the measured five years, else by year."""
    if (end - start).days + 1 <= MEASURED_RANGE_DAYS:
        return [(start, end)]
    spans, first = [], start
    while first <= end:
        last = min(dt.date(first.year, 12, 31), end)
        spans.append((first, last))
        first = last + dt.timedelta(days=1)
    return spans


def fetch_history(start: dt.date, end: dt.date, *, base_url: str, fetch) -> Fetched:
    """Request the history, and land every publication date returned, once, or nothing.

    Each calendar date in the range that no request returned is logged `absent_no_publication`,
    as a single-date request for a weekend is, because the count of unpublished dates is read
    from `ops.feed_request` (architecture.md). A date two yearly requests both return, the
    publication a later year's request snaps back to, lands once; if they disagree on it, the
    response is refused. A failed request fails the whole history, and the dates other requests
    answered are logged `discarded`, as a failed daily interval's are.
    """
    from nordbank_ops.feeds.fetch import FetchFailedError

    out = Fetched()
    seen: dict[tuple[dt.date, str], decimal.Decimal] = {}
    covered: list[tuple[dt.date, dt.date, RequestOutcome]] = []
    for first, last in history_requests(start, end):
        key = f"{first.isoformat()}..{last.isoformat()}"
        try:
            result = fetch(range_url(base_url, first, last), params=QUERY)
        except FetchFailedError as exc:
            attempt = exc.result.attempts[-1] if exc.result.attempts else None
            status = attempt.status if attempt else None
            outcome = RequestOutcome(key, FAILED, status, exc.result.attempt_count, detail=str(exc))
            out.outcomes.append(outcome)
            out.failure = f"the history request {key} failed: {exc}"
            break
        if not result.ok:
            detail = f"HTTP {result.status}"
            outcome = RequestOutcome(key, FAILED, result.status, result.attempt_count, 0, detail)
            out.outcomes.append(outcome)
            out.failure = f"the history request {key} answered {detail}"
            break
        records, payloads, drift, breaking = parse_range(result.body, first, last)
        out.parsed.drift.extend(drift)
        if breaking:
            out.parsed.breaking = breaking
        kept = 0
        for record, payload in zip(records, payloads, strict=True):
            identity = (record["rate_date"], record["quote_currency"])
            if identity in seen:
                if seen[identity] != record["rate"]:
                    out.parsed.breaking = (
                        f"two history requests disagree on {identity[1]} for {identity[0]}"
                    )
                continue
            seen[identity] = record["rate"]
            out.parsed.records.append(record)
            out.parsed.payloads.append(payload)
            kept += 1
        dates = sorted({record["rate_date"] for record in records})
        if dates:
            detail = f"{len(dates)} publication date(s), {dates[0]} to {dates[-1]}"
        else:
            detail = "no publication in the range; nothing landed"
        outcome = RequestOutcome(
            key, LANDED if kept else ABSENT, result.status, result.attempt_count, kept, detail
        )
        out.outcomes.append(outcome)
        covered.append((first, last, outcome))

    if out.failure:
        out.parsed.records, out.parsed.payloads = [], []
        for outcome in out.outcomes:
            if outcome.outcome == LANDED:
                outcome.outcome, outcome.rows = DISCARDED, 0
                outcome.detail = "answered; not landed, because the history failed"
        return out

    returned = {day for day, _currency in seen}
    for first, last, request in covered:
        for offset in range((last - first).days + 1):
            day = first + dt.timedelta(days=offset)
            if day not in returned:
                out.outcomes.append(
                    RequestOutcome(
                        day.isoformat(),
                        ABSENT,
                        request.status,
                        request.attempts,
                        0,
                        f"not in the time-series response {request.key}; nothing landed",
                    )
                )
    return out
