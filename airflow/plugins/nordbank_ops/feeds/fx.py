"""The ECB reference rates, one request per date (spec 006 section 2).

**The landing rule is the whole control.** Asked for a date the ECB did not publish — a weekend,
a TARGET holiday, a date after the latest publication — Frankfurter v1 does not refuse: it
answers HTTP 200 with the previous publication's rates and that publication's date. Measured on
2026-09-22 for Saturday 2026-07-25, which came back dated 2026-07-24, and for 2026-09-23, the
day after the measurement, which came back dated 2026-09-22. Only a date beyond all data
returns 404. So a response is landed only when its `date` equals the date requested; otherwise
nothing is written for the date, which is how bronze records a gap: as an absence, with the
request logged as `absent` in `ops.feed_request`. Carrying the last rate forward is silver's
job, and doing it here would destroy the evidence that the gap existed.

The same rule makes a rate for a simulated day ahead of the real clock impossible to fabricate:
the API answers such a request with an earlier date, and the rule lands nothing.

Rates are parsed from the JSON as decimals and never pass through a float.
"""

from __future__ import annotations

import datetime as dt
import decimal
import json
from dataclasses import dataclass, field

from nordbank_ops.feeds.land import Parsed

LANDED = "landed"
ABSENT = "absent"
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
    expected = contract.format["top_level"]
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


def fetch_dates(dates: list[dt.date], contract, *, base_url: str, fetch) -> Fetched:
    """Request every date, and refuse the whole interval if any request fails.

    Nothing is written until every request has an answer, so a partially fetched interval
    lands nothing and its batch is failed with the dates that did not answer; the watermark
    stays where it was and the next run requests the whole interval again.
    """
    from nordbank_ops.feeds.fetch import FetchFailedError

    out = Fetched()
    for requested in dates:
        url = f"{base_url.rstrip('/')}/{requested.isoformat()}"
        try:
            result = fetch(url, params={"base": "EUR"})
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
        detail = None if records else "the response is for an earlier publication date"
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
