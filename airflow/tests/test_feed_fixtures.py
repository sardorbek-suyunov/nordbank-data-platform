"""The feed rules against responses recorded from the real APIs (spec 006 section 6).

No test may require network access, so the rules that depend on how a third party behaves are
proven against recordings of it. Every recording carries a sidecar saying when and from where
it was captured, which contract version it was validated against, and why it was recorded; the
FRED keyed response is the one exception, taken from the publisher's documentation because no
key is held, and its sidecar says so.

What this file can prove and what it cannot. It proves that the platform handles what the APIs
sent when they were recorded. It cannot notice that an API has since changed: that is
`make feeds-probe`, which calls the live endpoints and compares shapes. **A fixture whose
contract fingerprint no longer matches the contract fails here**, because that is a change
somebody made in this repository; a fixture that is merely old does not, because a required
check that turns red on the calendar teaches people to ignore it, and its age is reported by
the probe instead.
"""

from __future__ import annotations

import datetime as dt
import decimal
import json
from pathlib import Path

import pytest
from data_contract import load_history
from nordbank_ops.feeds import fred, fx

ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "feeds"
CONTRACTS = ROOT / "contracts"


def _fixtures() -> list[str]:
    names = sorted(p.name[: -len(".meta.json")] for p in FIXTURES.glob("*.meta.json"))
    assert len(names) >= 8, f"{len(names)} recorded fixture(s) found under {FIXTURES}"
    return names


def _load(name: str) -> tuple[bytes, dict]:
    return (FIXTURES / f"{name}.body").read_bytes(), json.loads(
        (FIXTURES / f"{name}.meta.json").read_text(encoding="utf-8")
    )


def _contract(dotted: str):
    system, entity = dotted.split(".")
    return load_history(CONTRACTS / system)[entity][-1]


def test_every_fixture_says_when_and_where_it_was_captured() -> None:
    for name in _fixtures():
        body, meta = _load(name)
        assert body, name
        for field in (
            "captured_at",
            "endpoint",
            "status",
            "why",
            "contract",
            "contract_fingerprint",
        ):
            assert field in meta, f"{name} sidecar lacks {field}"
        dt.datetime.fromisoformat(meta["captured_at"])
        assert meta["endpoint"].startswith("https://"), name


@pytest.mark.parametrize("name", [
    "fx_2026-07-24", "fx_2026-07-25", "fx_2026-05-01", "fx_2026-04-03", "fx_2026-12-25",
    "fx_v2_rates", "fred_without_key", "fred_documented_example",
])  # fmt: skip
def test_every_fixture_was_validated_against_the_contract_in_force(name) -> None:
    _body, meta = _load(name)
    contract = _contract(meta["contract"])
    assert meta["contract_fingerprint"] == contract.fingerprint, (
        f"{name} was recorded against {meta['contract']} {meta['contract_fingerprint']} and the "
        f"contract is now {contract.fingerprint}: re-record with `make feeds-probe RECORD=1`"
    )


def test_a_recorded_publication_date_lands_every_currency_as_a_decimal() -> None:
    body, meta = _load("fx_2026-07-24")
    assert meta["status"] == 200
    records, payloads, drift, breaking = fx.parse_response(
        body, dt.date(2026, 7, 24), _contract("ecb.fx_rates")
    )
    assert breaking is None and drift == []
    assert len(records) == 29
    usd = next(r for r in records if r["quote_currency"] == "USD")
    assert usd["rate"] == decimal.Decimal("1.1377")
    assert all(isinstance(r["rate"], decimal.Decimal) for r in records)
    assert len(set(payloads)) == 1


@pytest.mark.parametrize(
    ("name", "requested", "served"),
    [
        ("fx_2026-07-25", dt.date(2026, 7, 25), "2026-07-24"),
        ("fx_2026-05-01", dt.date(2026, 5, 1), "2026-04-30"),
        ("fx_2026-04-03", dt.date(2026, 4, 3), "2026-04-02"),
    ],
)
def test_a_recorded_unpublished_date_is_answered_with_an_earlier_one_and_lands_nothing(
    name, requested, served
) -> None:
    """The weekend and both holidays: HTTP 200, an earlier date, and nothing landed."""
    body, meta = _load(name)
    assert meta["status"] == 200
    assert json.loads(body)["date"] == served
    records, payloads, _drift, breaking = fx.parse_response(
        body, requested, _contract("ecb.fx_rates")
    )
    assert (records, payloads, breaking) == ([], [], None)


def test_a_recorded_date_beyond_all_data_is_a_404() -> None:
    _body, meta = _load("fx_2026-12-25")
    assert meta["status"] == 404


def test_the_recorded_deprecation_is_the_one_the_contract_names() -> None:
    _body, meta = _load("fx_2026-07-24")
    headers = {k.lower(): v for k, v in meta["headers"].items()}
    assert headers["deprecation"] == "@1779103800"
    assert dt.datetime.fromtimestamp(1779103800, dt.UTC).date() == dt.date(2026, 5, 18)
    assert "v2/rates" in headers["link"]


def test_the_recorded_v2_response_would_be_refused_as_a_shape_change() -> None:
    body, meta = _load("fx_v2_rates")
    assert meta["status"] == 200
    rows = json.loads(body)
    assert isinstance(rows, list) and len({row["date"] for row in rows}) >= 2, (
        "v2 dates currencies on different days for a weekend request"
    )
    _r, _p, _drift, breaking = fx.parse_response(
        body, dt.date(2026, 7, 25), _contract("ecb.fx_rates")
    )
    assert breaking == "response shape changed: not a JSON object"


def test_the_recorded_keyless_fred_response_is_why_the_feed_skips() -> None:
    body, meta = _load("fred_without_key")
    assert meta["status"] == 400
    assert "api_key" in json.loads(body)["error_message"]
    assert fred.key_absent_reason({}) is not None


def test_the_documented_fred_example_parses_under_the_contract() -> None:
    body, meta = _load("fred_documented_example")
    assert meta["source"] == "documentation"
    parsed = fred.parse_observations(body, "GNPCA", _contract("fred.series"))
    assert parsed.breaking is None and len(parsed.records) == 2
    assert parsed.records[0]["value"] == decimal.Decimal("1065.9")
