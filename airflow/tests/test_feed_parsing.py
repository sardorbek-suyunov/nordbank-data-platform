"""Parsing and landing the feeds, with no network, no stack and no Airflow (spec 006).

The Frankfurter bodies here are the responses measured on 2026-09-22 and quoted in the
contract; `test_feed_fixtures.py` runs the same rules against the recorded files.
"""

from __future__ import annotations

import csv
import datetime as dt
import decimal
import io
import json
import random
import sys
from pathlib import Path

import pytest
from data_contract import load_history
from nordbank_ops.feeds import cast as casting
from nordbank_ops.feeds import clearing, fred, fx, sanctions
from nordbank_ops.feeds.fetch import Attempt, FetchResult
from nordbank_ops.feeds.land import CleartextInPayloadError, Parsed, _guard
from nordbank_ops.tokenise import Tokeniser

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
CONTRACTS = ROOT / "contracts"
TOKENISER = Tokeniser(salt=b"parsing-test-salt")

FRIDAY = b'{"amount":1.0,"base":"EUR","date":"2026-07-24","rates":{"GBP":0.8561,"USD":1.1377}}'


def _contract(system: str, entity: str):
    return load_history(CONTRACTS / system)[entity][-1]


# --- casting ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "declared", "expected"),
    [
        ("12.3400", "numeric(18,4)", decimal.Decimal("12.3400")),
        ("-4.5000", "numeric(18,4)", decimal.Decimal("-4.5000")),
        ("2026-08-03", "date", dt.date(2026, 8, 3)),
        ("07", "integer", 7),
        ("", "numeric(18,4)", None),
        ("NB0000000001", "character(12)", "NB0000000001"),
    ],
)
def test_a_well_formed_value_casts(raw, declared, expected) -> None:
    assert casting.cast(raw, declared) == expected


@pytest.mark.parametrize(
    ("raw", "declared"),
    [
        ("12.3400EUR", "numeric(18,4)"),
        ("12,34", "numeric(18,4)"),
        ("1e3", "numeric(18,4)"),
        ("2026-13-05", "date"),
        ("2026-02-30", "date"),
        ("05/08/2026", "date"),
        ("7.0", "integer"),
    ],
)
def test_a_malformed_value_is_refused_not_repaired(raw, declared) -> None:
    with pytest.raises(casting.CastError):
        casting.cast(raw, declared)


def test_a_declared_null_marker_is_a_null() -> None:
    assert casting.cast(".", "numeric(18,8)", null_markers=(".",)) is None
    with pytest.raises(casting.CastError):
        casting.cast(".", "numeric(18,8)")


# --- the FX landing rule ---------------------------------------------------------------------


def test_a_response_for_the_date_asked_lands_one_row_per_currency() -> None:
    records, payloads, drift, breaking = fx.parse_response(
        FRIDAY, dt.date(2026, 7, 24), _contract("ecb", "fx_rates")
    )
    assert breaking is None and drift == []
    assert [r["quote_currency"] for r in records] == ["GBP", "USD"]
    assert records[1]["rate"] == decimal.Decimal("1.1377")
    assert isinstance(records[1]["rate"], decimal.Decimal)
    assert payloads == [FRIDAY.decode()] * 2


def test_a_response_dated_before_the_date_asked_lands_nothing() -> None:
    """Saturday 2026-07-25 was answered with Friday's rates, dated Friday."""
    records, payloads, _drift, breaking = fx.parse_response(
        FRIDAY, dt.date(2026, 7, 25), _contract("ecb", "fx_rates")
    )
    assert (records, payloads, breaking) == ([], [], None)


def test_a_changed_response_shape_is_breaking() -> None:
    reshaped = b'[{"date":"2026-07-24","base":"EUR","quote":"USD","rate":1.1377}]'
    body = json.dumps({"base": "EUR", "date": "2026-07-24", "rates": {}}).encode()
    _r, _p, drift, breaking = fx.parse_response(
        body, dt.date(2026, 7, 24), _contract("ecb", "fx_rates")
    )
    assert breaking and "amount" in breaking
    assert any(o["kind"] == "removed_column" for o in drift)
    _r, _p, drift, breaking = fx.parse_response(
        reshaped, dt.date(2026, 7, 24), _contract("ecb", "fx_rates")
    )
    assert breaking == "response shape changed: not a JSON object"
    assert drift[0]["kind"] == "type_change"


def test_the_dates_to_fetch_follow_the_watermark() -> None:
    day = dt.date(2026, 8, 3)
    assert fx.dates_to_fetch(None, day) == [day]
    assert fx.dates_to_fetch(dt.date(2026, 8, 2), day) == [day]
    assert fx.dates_to_fetch(dt.date(2026, 7, 31), day) == [
        dt.date(2026, 8, 1),
        dt.date(2026, 8, 2),
        day,
    ]
    assert fx.dates_to_fetch(day, day) == [day]


def _scripted(outcomes: dict):
    def fetch(url, params=None):
        date = url.rsplit("/", 1)[1]
        outcome = outcomes[date]
        if isinstance(outcome, Exception):
            raise outcome
        status, body = outcome
        return FetchResult(url=url, status=status, body=body, attempts=[Attempt(status, None, 0.0)])

    return fetch


def test_an_interval_with_one_failed_date_lands_nothing_and_says_which() -> None:
    from nordbank_ops.feeds.fetch import FetchFailedError

    failure = FetchResult(url="u", status=503, body=None)
    failure.attempts = [Attempt(503, None, 1.0) for _ in range(5)]
    fetch = _scripted(
        {
            "2026-07-24": (200, FRIDAY),
            "2026-07-25": (200, FRIDAY),
            "2026-07-26": FetchFailedError(failure),
        }
    )
    dates = [dt.date(2026, 7, 24), dt.date(2026, 7, 25), dt.date(2026, 7, 26)]
    fetched = fx.fetch_dates(
        dates, _contract("ecb", "fx_rates"), base_url="https://x/v1", fetch=fetch
    )
    assert fetched.failure and "2 of 3 date(s) answered" in fetched.failure
    assert fetched.parsed.records == []
    assert [o.outcome for o in fetched.outcomes] == ["landed", "absent", "failed"]


def test_a_date_beyond_all_data_is_absent_not_failed() -> None:
    fetch = _scripted({"2026-12-25": (404, b'{"message":"not found"}')})
    fetched = fx.fetch_dates(
        [dt.date(2026, 12, 25)], _contract("ecb", "fx_rates"), base_url="https://x/v1", fetch=fetch
    )
    assert fetched.failure is None
    assert [o.outcome for o in fetched.outcomes] == ["absent"]


# --- the clearing file -----------------------------------------------------------------------


def _clearing(malformed: float = 0.0, columns=None) -> bytes:
    from generator.settlement import build, timeline

    items = []
    for n in range(30):
        items.append(
            build.Item(
                transaction_reference=f"TXN{n:013d}",
                transaction_date=dt.date(2026, 8, 2),
                clearing_date=dt.date(2026, 8, 2),
                network="visa" if n % 3 else "mastercard",
                card_reference=f"NB{n:010d}",
                card_bin="400001",
                card_last_four=f"{n:04d}",
                merchant_category_code="5411",
                merchant_name=f"Shop, number {n}",
                is_card_present=bool(n % 2),
                settlement_currency="EUR",
                settlement_amount=decimal.Decimal(f"{10 + n}.2500"),
            )
        )
    parameters = build.Parameters(
        processor_id="NBKPROC",
        malformed_record_share=malformed,
        malformed_kind_mix={
            build.UNPARSEABLE_AMOUNT: 0.25,
            build.INVALID_DATE: 0.25,
            build.MISSING_REQUIRED_FIELD: 0.25,
            build.WRONG_FIELD_COUNT: 0.25,
        },
        break_warn_share=0.0,
        break_error_share=0.0,
        break_warn_relative=(0.0001, 0.0008),
        break_error_relative=(0.005, 0.05),
    )
    return build.build(
        settlement_date=dt.date(2026, 8, 3),
        items=items,
        columns=columns or timeline.BASE_COLUMNS,
        parameters=parameters,
        rng=random.Random(3),
        ledger_totals={},
    ).body


def _read(body: bytes):
    return clearing.read(
        body,
        _contract("cardnet", "settlements"),
        _contract("cardnet", "settlement_totals"),
        TOKENISER,
    )


def test_a_clean_file_reads_every_record_with_its_settlement_date() -> None:
    read = _read(_clearing())
    assert read.structural_fault is None and read.details.breaking is None
    assert len(read.details.records) == 30 and read.details.refused == []
    assert {r["settlement_date"] for r in read.details.records} == {dt.date(2026, 8, 3)}
    assert len(read.totals.records) == 2
    assert sum(r["record_count"] for r in read.totals.records) == 30


def test_the_payload_carries_the_card_token_and_never_the_card_reference() -> None:
    read = _read(_clearing())
    for record, payload in zip(read.details.records, read.details.payloads, strict=True):
        fields = next(csv.reader(io.StringIO(payload)))
        assert record["card_reference"] not in payload
        assert TOKENISER.token(record["card_reference"]) in fields
        assert len(fields) == 12
    _guard(read.details.payloads, read.card_references)


def test_the_guard_refuses_a_payload_that_kept_an_identifier() -> None:
    with pytest.raises(CleartextInPayloadError):
        _guard(["D,TXN1,NB0000000001"], ["NB0000000001"])


def test_malformed_records_are_refused_individually_with_their_reasons() -> None:
    read = _read(_clearing(malformed=0.4))
    assert read.structural_fault is None
    refused = read.details.refused
    assert len(refused) >= 4
    reasons = {rejection.reason for _record, rejection, _payload in refused}
    assert any(reason.startswith("record has") for reason in reasons)
    assert "value does not parse as its declared type" in reasons
    for _record, _rejection, payload in refused:
        assert "NB00000000" not in payload


def test_an_extra_column_is_additive_and_a_removed_one_is_breaking() -> None:
    from generator.settlement import timeline

    added = (*timeline.BASE_COLUMNS, "interchange_fee_amount")
    read = _read(_clearing(columns=added))
    assert read.details.breaking is None
    assert [o["kind"] for o in read.details.drift] == ["additive"]
    assert len(read.details.records) == 30

    removed = tuple(c for c in added if c != "merchant_name")
    read = _read(_clearing(columns=removed))
    assert read.details.breaking == "removed column(s) merchant_name"
    assert read.totals.breaking == read.details.breaking


def test_a_reordered_column_line_is_breaking() -> None:
    from generator.settlement import timeline

    swapped = list(timeline.BASE_COLUMNS)
    swapped[2], swapped[3] = swapped[3], swapped[2]
    read = _read(_clearing(columns=tuple(swapped)))
    assert read.details.breaking == "the contract's fields arrive in a different order"


@pytest.mark.parametrize(
    ("mutate", "fault"),
    [
        (lambda lines: lines[1:], "the first line is not a header record"),
        (lambda lines: lines[:-1], "the last line is not an end record"),
        (lambda lines: [*lines[:-1], "Z,999"], "the end record declares 999"),
    ],
)
def test_a_structurally_broken_file_is_refused_whole(mutate, fault) -> None:
    lines = _clearing().decode().splitlines()
    read = _read(("\n".join(mutate(lines)) + "\n").encode())
    assert read.structural_fault and read.structural_fault.startswith(fault)


# --- the sanctions snapshot ------------------------------------------------------------------


def test_a_snapshot_reads_every_entity_with_its_version() -> None:
    from generator.sanctions import build

    publication = build.publish(
        42,
        dt.date(2026, 7, 20),
        dt.date(2026, 7, 21),
        build.Parameters(base_entities=20, weekly_additions=2, weekly_removals=1, pep_share=0.3),
    )
    contract = _contract("opensanctions", "entities")
    index = sanctions.read_index(publication.index, "opensanctions/sanctions/", contract)
    assert index.version == publication.version
    assert index.published_at == dt.datetime(2026, 7, 21, 7, 0, tzinfo=dt.UTC)
    parsed = sanctions.read_entities(publication.entities, index, contract)
    assert parsed.breaking is None and parsed.refused == []
    assert len(parsed.records) == publication.entity_count >= 20
    assert all(r["publisher_version"] == publication.version for r in parsed.records)
    assert all(isinstance(r["names"], list) for r in parsed.records)


def test_a_snapshot_missing_a_documented_key_is_breaking() -> None:
    line = json.dumps({"id": "NK-1", "schema": "Person", "caption": "ZZ-X SANCTIONS-FIXTURE"})
    index = sanctions.Index("v", dt.datetime(2026, 7, 21, tzinfo=dt.UTC), "k")
    parsed = sanctions.read_entities(line.encode(), index, _contract("opensanctions", "entities"))
    assert parsed.breaking and "properties" in parsed.breaking


# --- FRED ------------------------------------------------------------------------------------


def test_the_macro_feed_states_why_it_is_skipped_without_a_key() -> None:
    assert fred.key_absent_reason({}) and "FRED_API_KEY" in fred.key_absent_reason({})
    assert fred.key_absent_reason({"FRED_API_KEY": "__EXTERNAL__"})
    assert fred.key_absent_reason({"FRED_API_KEY": "abc"}) is None


def test_a_fred_null_marker_lands_as_a_null() -> None:
    body = json.dumps(
        {
            **{key: "x" for key in _contract("fred", "series").format["top_level"]},
            "observations": [
                {
                    "realtime_start": "2026-09-01",
                    "realtime_end": "9999-12-31",
                    "date": "2026-07-01",
                    "value": "4.3",
                },
                {
                    "realtime_start": "2026-09-01",
                    "realtime_end": "9999-12-31",
                    "date": "2026-08-01",
                    "value": ".",
                },
            ],
        }
    ).encode()
    parsed = fred.parse_observations(body, "UNRATE", _contract("fred", "series"))
    assert [r["value"] for r in parsed.records] == [decimal.Decimal("4.3"), None]
    assert Parsed is not None
