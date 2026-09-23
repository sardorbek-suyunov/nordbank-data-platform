"""FRED series observations, optional (spec 006 section 2).

**Skipped with a stated reason when there is no key.** FRED requires a free API key; measured,
a keyless request answers HTTP 400 with the message that `api_key` is not set. The DAG checks
for the key first and skips everything downstream with that reason rather than failing, so a
deployment without a key is green and says why, and nothing in CI depends on the feed.

With a key, one request per configured series, all of a series' observations each time.
Revisions to an already published period arrive as the same observation date under a later
`realtime_start`; they land as new rows in a new batch and silver keeps the history.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from nordbank_ops.feeds.cast import CastError, cast
from nordbank_ops.feeds.fx import FAILED, LANDED, RequestOutcome
from nordbank_ops.feeds.land import Parsed
from nordbank_ops.validation import QuarantineReason, Rejection

KEY_VARIABLE = "FRED_API_KEY"
SERIES_VARIABLE = "FRED_SERIES"
ENDPOINT = "https://api.stlouisfed.org/fred/series/observations"
SENTINELS = ("", "__EXTERNAL__")


def key_absent_reason(environ=None) -> str | None:
    """Why the feed cannot run, or None when it can."""
    source = os.environ if environ is None else environ
    if source.get(KEY_VARIABLE, "").strip() in SENTINELS:
        return (
            f"{KEY_VARIABLE} is not set. FRED requires a free API key and answers a keyless "
            "request with HTTP 400, so the macro series feed is skipped rather than failed. "
            "Nothing else in the platform depends on it; set the key in .env to enable it."
        )
    return None


def series(environ=None) -> list[str]:
    source = os.environ if environ is None else environ
    return [s.strip() for s in source.get(SERIES_VARIABLE, "").split(",") if s.strip()]


@dataclass
class Fetched:
    parsed: Parsed = field(default_factory=Parsed)
    outcomes: list[RequestOutcome] = field(default_factory=list)
    failure: str | None = None


def parse_observations(body: bytes, series_id: str, contract) -> Parsed:
    out = Parsed()
    response = json.loads(body.decode("utf-8"))
    missing = [key for key in contract.format["top_level"] if key not in response]
    if missing:
        out.breaking = f"response key(s) absent: {missing}"
        return out
    markers = tuple(contract.format.get("null_markers", ()))
    for observation in response["observations"]:
        payload = json.dumps(observation, sort_keys=True)
        record = {"series_id": series_id}
        try:
            record["observation_date"] = cast(observation.get("date"), "date")
            record["realtime_start"] = cast(observation.get("realtime_start"), "date")
            record["realtime_end"] = cast(observation.get("realtime_end"), "date")
            record["value"] = cast(observation.get("value"), "numeric(18,8)", null_markers=markers)
        except CastError:
            rejection = Rejection("value", QuarantineReason.TYPE_MISMATCH, observation, None)
            out.refused.append((record, rejection, payload))
            continue
        out.records.append(record)
        out.payloads.append(payload)
    return out


def fetch_series(names: list[str], contract, *, api_key: str, fetch) -> Fetched:
    """Every series, all or nothing, exactly as the FX feed treats its dates."""
    from nordbank_ops.feeds.fetch import FetchFailedError

    out = Fetched()
    for name in names:
        params = {"series_id": name, "api_key": api_key, "file_type": "json"}
        try:
            result = fetch(ENDPOINT, params=params)
        except FetchFailedError as exc:
            out.outcomes.append(
                RequestOutcome(name, FAILED, None, exc.result.attempt_count, detail=str(exc))
            )
            continue
        if not result.ok:
            out.outcomes.append(
                RequestOutcome(
                    name,
                    FAILED,
                    result.status,
                    result.attempt_count,
                    detail=f"HTTP {result.status}",
                )
            )
            continue
        parsed = parse_observations(result.body, name, contract)
        if parsed.breaking:
            out.parsed.breaking = parsed.breaking
        out.parsed.records.extend(parsed.records)
        out.parsed.payloads.extend(parsed.payloads)
        out.parsed.refused.extend(parsed.refused)
        out.outcomes.append(
            RequestOutcome(name, LANDED, result.status, result.attempt_count, len(parsed.records))
        )
    failed = [o for o in out.outcomes if o.outcome == FAILED]
    if failed:
        out.failure = f"{len(failed)} of {len(names)} series failed: " + "; ".join(
            f"{o.key}: {o.detail}" for o in failed
        )
        out.parsed = Parsed()
    return out
