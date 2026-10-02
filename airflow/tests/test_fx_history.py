"""The FX history through the time-series endpoint: every returned date lands, none is filled.

The parsing rules run against synthetic responses in the shape measured on 2026-10-02; the open
step runs against a real DuckDB warehouse with the schema applied and the real contract tree.
The recorded `ci` range is exercised in `test_feed_fixtures.py`.
"""

from __future__ import annotations

import datetime as dt
import decimal
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest
from nordbank_ops import registry
from nordbank_ops.feeds import fx, phases
from nordbank_ops.feeds.fetch import FetchFailedError, FetchResult

ROOT = Path(__file__).resolve().parent.parent.parent
SCHEMA_DIR = ROOT / "infra" / "warehouse" / "schema"
CONTRACTS = ROOT / "contracts"
BASE = "https://api.frankfurter.dev/v1"


def _body(rates: dict[str, dict[str, str]], start: str, end: str) -> bytes:
    """A response in the measured shape, numbers written as the publisher writes them."""
    days = ",".join(
        f'"{day}":{{' + ",".join(f'"{c}":{v}' for c, v in by.items()) + "}"
        for day, by in rates.items()
    )
    return (
        f'{{"amount":1.0,"base":"EUR","start_date":"{start}","end_date":"{end}",'
        f'"rates":{{{days}}}}}'
    ).encode()


# Thursday, Friday, then Monday: the weekend is not returned, and must not be landed.
WEEK = {
    "2026-07-16": {"USD": "1.1401", "GBP": "0.8512", "JPY": "185.2"},
    "2026-07-17": {"USD": "1.1390", "GBP": "0.85", "JPY": "185.54"},
    "2026-07-20": {"USD": "1.1426", "GBP": "0.84888", "JPY": "185.54"},
}


def test_every_returned_date_lands_and_no_other_date_is_filled() -> None:
    body = _body(WEEK, "2026-07-16", "2026-07-20")
    records, payloads, drift, breaking = fx.parse_range(
        body, dt.date(2026, 7, 16), dt.date(2026, 7, 20)
    )
    assert breaking is None and drift == []
    assert len(records) >= 9
    landed = sorted({r["rate_date"] for r in records})
    assert landed == [dt.date(2026, 7, 16), dt.date(2026, 7, 17), dt.date(2026, 7, 20)]
    assert len(payloads) == len(records)
    assert all(isinstance(r["rate"], decimal.Decimal) for r in records)
    usd = next(
        r
        for r in records
        if r["rate_date"] == dt.date(2026, 7, 20) and r["quote_currency"] == "USD"
    )
    assert usd["rate"] == decimal.Decimal("1.1426") and usd["base_currency"] == "EUR"


def test_the_publication_a_range_snaps_back_to_is_landed() -> None:
    """Asked from Saturday 2026-07-18, Frankfurter starts at Friday's publication (measured)."""
    rates = {day: WEEK[day] for day in ("2026-07-17", "2026-07-20")}
    body = _body(rates, "2026-07-17", "2026-07-20")
    records, _p, _d, breaking = fx.parse_range(body, dt.date(2026, 7, 18), dt.date(2026, 7, 20))
    assert breaking is None
    assert len(records) >= 6
    assert min(r["rate_date"] for r in records) == dt.date(2026, 7, 17)


REFUSALS = [
    ("a date after the requested end", "2026-07-16", "2026-07-17"),
    ("two dates before the requested start", "2026-07-18", "2026-07-20"),
    ("an earlier date although the start was published", "2026-07-17", "2026-07-20"),
]


def test_the_refusal_cases_cover_both_ends() -> None:
    assert len(REFUSALS) >= 3


@pytest.mark.parametrize(("start", "end"), [c[1:] for c in REFUSALS], ids=[c[0] for c in REFUSALS])
def test_a_date_outside_the_request_refuses_the_whole_response(start, end) -> None:
    rates = dict(WEEK)
    if start == "2026-07-18":
        rates = {"2026-07-15": WEEK["2026-07-16"], **WEEK}
    body = _body(rates, min(rates), max(rates))
    records, payloads, _drift, breaking = fx.parse_range(
        body, dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    )
    assert (records, payloads) == ([], [])
    assert breaking and breaking.startswith("answered with dates outside")


SHAPES = [
    ("a list", b'[{"date":"2026-07-20","quote":"USD","rate":1.1426}]', "not a JSON object"),
    (
        "no start_date",
        b'{"amount":1.0,"base":"EUR","end_date":"2026-07-20","rates":{}}',
        "missing ['start_date']",
    ),
    (
        "a rate that is text",
        b'{"amount":1.0,"base":"EUR","start_date":"2026-07-20","end_date":"2026-07-20",'
        b'"rates":{"2026-07-20":{"USD":"1.1426"}}}',
        "are not numbers",
    ),
]


def test_the_shape_cases_cover_the_envelope_and_the_rates() -> None:
    assert len(SHAPES) >= 3


@pytest.mark.parametrize(("body", "reason"), [c[1:] for c in SHAPES], ids=[c[0] for c in SHAPES])
def test_a_changed_shape_refuses_the_whole_response(body, reason) -> None:
    records, _p, _d, breaking = fx.parse_range(body, dt.date(2026, 7, 20), dt.date(2026, 7, 20))
    assert records == []
    assert breaking and reason in breaking


def test_each_payload_is_its_own_date_in_the_digits_received() -> None:
    body = _body(WEEK, "2026-07-16", "2026-07-20")
    records, payloads, _d, _b = fx.parse_range(body, dt.date(2026, 7, 16), dt.date(2026, 7, 20))
    assert len(set(payloads)) == 3
    for record, payload in zip(records, payloads, strict=True):
        parsed = json.loads(payload, parse_float=decimal.Decimal)
        day = record["rate_date"].isoformat()
        assert list(parsed) == ["amount", "base", "start_date", "end_date", "rates"]
        assert list(parsed["rates"]) == [day]
        assert parsed["rates"][day][record["quote_currency"]] == record["rate"]
    # A trailing zero is a digit the publisher sent, and the slice keeps it.
    assert '"GBP":0.85,' in next(p for p in payloads if "2026-07-17" in p)
    assert '"amount":1.0,' in payloads[0]


def _fetcher(status: int, body: bytes = b""):
    calls = []

    def fetch(url, params=None):
        calls.append((url, params))
        return FetchResult(url=url, status=status, body=body)

    return fetch, calls


def test_the_history_is_one_request_to_the_time_series_endpoint() -> None:
    fetch, calls = _fetcher(200, _body(WEEK, "2026-07-16", "2026-07-20"))
    fetched = fx.fetch_range(dt.date(2026, 7, 16), dt.date(2026, 7, 20), base_url=BASE, fetch=fetch)
    assert calls == [(f"{BASE}/2026-07-16..2026-07-20", {"base": "EUR"})]
    assert fetched.failure is None
    assert len(fetched.parsed.records) >= 9
    (outcome,) = fetched.outcomes
    assert outcome.outcome == fx.LANDED and outcome.rows == len(fetched.parsed.records)
    assert outcome.detail == "3 publication date(s), 2026-07-16 to 2026-07-20"
    assert fx.range_requested_as(BASE, dt.date(2026, 7, 16), dt.date(2026, 7, 20)) == (
        f"{BASE}/2026-07-16..2026-07-20?base=EUR"
    )


def test_a_failed_history_request_lands_nothing() -> None:
    fetch, _calls = _fetcher(404)
    fetched = fx.fetch_range(dt.date(2026, 7, 16), dt.date(2026, 7, 20), base_url=BASE, fetch=fetch)
    assert fetched.failure and "HTTP 404" in fetched.failure
    assert fetched.parsed.records == []

    def exhausted(url, params=None):
        raise FetchFailedError(FetchResult(url=url, status=521, body=b""))

    fetched = fx.fetch_range(
        dt.date(2026, 7, 16), dt.date(2026, 7, 20), base_url=BASE, fetch=exhausted
    )
    assert fetched.failure and fetched.parsed.records == []
    assert fetched.outcomes[0].outcome == fx.FAILED


# --- the open step -----------------------------------------------------------------------------


@pytest.fixture
def warehouse(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "warehouse.duckdb"
    connection = duckdb.connect(str(path))
    for sql in sorted(SCHEMA_DIR.glob("*.sql")):
        code = "\n".join(
            line
            for line in sql.read_text(encoding="utf-8").splitlines()
            if not line.strip().startswith("--")
        )
        for statement in code.split(";"):
            if statement.strip():
                connection.execute(statement)
    connection.close()
    monkeypatch.setenv("DUCKDB_PATH", str(path))
    root = tmp_path / "contracts"
    shutil.copytree(CONTRACTS, root)
    monkeypatch.setattr("nordbank_ops.ingest.contract_root", lambda: root)
    return path


def _context(day: dt.date, conf: dict | None = None) -> dict:
    logical = dt.datetime.combine(day, dt.time(), tzinfo=dt.UTC)
    run = SimpleNamespace(logical_date=logical, run_id=f"manual__{day}", conf=conf or {})
    return {"dag_run": run}


HISTORY = {"history": {"from": "2024-01-23", "to": "2026-07-19"}}


def test_the_history_opens_one_batch_and_never_twice(warehouse) -> None:
    (unit,) = phases.fx_open(_context(dt.date(2026, 7, 19), HISTORY))
    assert unit["range"] == ["2024-01-23", "2026-07-19"]
    (batch,) = unit["batches"]
    assert batch["batch_id"] == "fx_rates-20260719T000000-01"

    connection = duckdb.connect(str(warehouse))
    try:
        connection.execute(
            "update ops.batch_registry set status = ? where batch_id = ?",
            [registry.REGISTERED, batch["batch_id"]],
        )
    finally:
        connection.close()
    assert phases.fx_open(_context(dt.date(2026, 7, 19), HISTORY)) == []


def test_a_first_daily_run_after_the_history_requests_its_own_date_only(warehouse) -> None:
    (unit,) = phases.fx_open(_context(dt.date(2026, 7, 20)))
    assert unit["dates"] == ["2026-07-20"]
