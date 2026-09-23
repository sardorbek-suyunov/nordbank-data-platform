"""Authored contracts for the external feeds (spec 006 section 5).

There is no dictionary entry for a third party's file or API, so these contracts name their
source of truth, describe the delivery's shape, and may key on more than one column. A contract
without a `kind` is still held to every field of specification 005's relational shape.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from data_contract import ContractError, load_history, parse
from nordbank_ops.validation import QuarantineReason, validate

CONTRACTS = Path(__file__).resolve().parent.parent.parent / "contracts"
FEEDS = {
    ("ecb", "fx_rates"): "api",
    ("cardnet", "settlements"): "file",
    ("cardnet", "settlement_totals"): "file",
    ("opensanctions", "entities"): "snapshot",
    ("fred", "series"): "api",
}


def _minimal(**overrides) -> dict:
    body = {
        "source_system": "ecb",
        "source_schema": "interval_api",
        "entity": "fx_rates",
        "kind": "api",
        "contract_version": 1,
        "in_force_from": None,
        "source_of_truth": {"document": "Frankfurter API v1 reference"},
        "primary_key": ["rate_date", "quote_currency"],
        "format": {"response": "json"},
        "columns": [
            {
                "name": "rate_date",
                "type": "date",
                "nullable": False,
                "classification": "non-personal",
            },
            {
                "name": "quote_currency",
                "type": "character(3)",
                "nullable": False,
                "classification": "non-personal",
            },
            {
                "name": "rate",
                "type": "numeric(18,8)",
                "nullable": False,
                "classification": "non-personal",
            },
        ],
    }
    body.update(overrides)
    return body


def test_every_feed_contract_is_committed_and_loads() -> None:
    found = {}
    for (system, entity), kind in FEEDS.items():
        chains = load_history(CONTRACTS / system)
        contract = chains[entity][-1]
        assert contract.kind == kind
        assert contract.is_authored
        assert contract.source_of_truth["document"]
        assert contract.watermark_column is None and contract.dictionary_revision is None
        assert len(contract.record_columns) >= 3
        found[(system, entity)] = contract
    assert len(found) == 5


def test_the_only_feed_identifier_is_the_card_reference() -> None:
    identifiers = {
        (system, entity): load_history(CONTRACTS / system)[entity][-1].identifier_columns()
        for system, entity in FEEDS
    }
    assert identifiers.pop(("cardnet", "settlements")) == ("card_reference",)
    assert all(columns == () for columns in identifiers.values()), identifiers


def test_a_composite_key_parses_and_is_described() -> None:
    contract = parse(_minimal(), "test")
    assert contract.keys == ("rate_date", "quote_currency")
    with pytest.raises(ContractError, match="key column 'missing'"):
        parse(_minimal(primary_key=["rate_date", "missing"]), "test")


@pytest.mark.parametrize("field", ["source_of_truth", "format", "kind", "in_force_from"])
def test_an_authored_contract_must_carry_its_fields(field) -> None:
    body = _minimal()
    del body[field]
    if field == "kind":
        # With no kind it is read as a relational contract, which it is not either.
        with pytest.raises(ContractError, match="missing required field"):
            parse(body, "test")
        return
    with pytest.raises(ContractError, match="missing required field"):
        parse(body, "test")


def test_a_source_of_truth_must_name_a_document() -> None:
    with pytest.raises(ContractError, match="source_of_truth must name"):
        parse(_minimal(source_of_truth={"publisher": "someone"}), "test")


def test_an_unknown_kind_is_refused() -> None:
    with pytest.raises(ContractError, match="kind 'stream'"):
        parse(_minimal(kind="stream"), "test")


def test_an_origin_is_refused_on_a_relational_contract() -> None:
    body = {
        "source_system": "corebank",
        "source_schema": "core",
        "entity": "accounts",
        "contract_version": 1,
        "watermark_column": "updated_at",
        "primary_key": "account_id",
        "dictionary_revision": "sha256:0123456789abcdef",
        "in_force_from": None,
        "columns": [
            {
                "name": "account_id",
                "type": "bigint",
                "nullable": False,
                "classification": "pseudonymous_key",
                "origin": "header",
            },
            {
                "name": "updated_at",
                "type": "timestamp with time zone",
                "nullable": False,
                "classification": "non-personal",
            },
        ],
    }
    with pytest.raises(ContractError, match="only an authored contract can"):
        parse(body, "test")


def test_a_composite_key_duplicated_within_a_batch_is_quarantined() -> None:
    import datetime as dt
    import decimal

    contract = parse(_minimal(), "test")
    day = dt.date(2026, 7, 24)
    rows = [
        {"rate_date": day, "quote_currency": "USD", "rate": decimal.Decimal("1.1377")},
        {"rate_date": day, "quote_currency": "GBP", "rate": decimal.Decimal("0.8561")},
        {"rate_date": day, "quote_currency": "USD", "rate": decimal.Decimal("1.1377")},
    ]
    result = validate(rows, contract)
    assert (result.rows_landed, result.rows_quarantined) == (2, 1)
    assert result.rejected[0][1].reason == QuarantineReason.PRIMARY_KEY_DUPLICATE


def test_the_shape_is_part_of_the_fingerprint() -> None:
    base = parse(_minimal(), "test")
    reshaped = parse(_minimal(format={"response": "json", "top_level": {"date": "date"}}), "test")
    assert base.fingerprint != reshaped.fingerprint
