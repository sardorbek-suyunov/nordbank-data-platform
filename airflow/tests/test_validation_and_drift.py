"""Unit tests for the ingest gate: per-record validation and per-schema drift classification.

Most of what these assert cannot happen against the core banking source, which is exactly why
they are asserted here. Postgres enforces every declared type, every primary key is unique and
not null, and the scripted drift timeline exercises one breaking kind of three. Spec 005
section 7 says so plainly rather than letting the coverage look complete; these tests are what
stands behind the modes the source cannot reach.

No database and no Airflow: these run in `make test`.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from data_contract import parse
from nordbank_ops.schema_drift import (
    ADDITIVE,
    COLUMN_REMOVED,
    PRIMARY_KEY_CHANGED,
    TYPE_CHANGED,
    classify,
)
from nordbank_ops.validation import (
    QuarantineReason,
    matches_type,
    project,
    validate,
    validate_record,
)

CONTRACT = parse(
    {
        "source_system": "corebank",
        "source_schema": "core",
        "entity": "accounts",
        "contract_version": 1,
        "watermark_column": "updated_at",
        "primary_key": "account_id",
        "dictionary_revision": "sha256:0123456789abcdef",
        "columns": [
            {
                "name": "account_id",
                "type": "bigint",
                "nullable": False,
                "classification": "pseudonymous_key",
            },
            {
                "name": "iban",
                "type": "character varying(34)",
                "nullable": True,
                "classification": "identifier",
            },
            {
                "name": "current_balance_amount",
                "type": "numeric(18,4)",
                "nullable": False,
                "classification": "non-personal",
            },
            {
                "name": "is_deleted",
                "type": "boolean",
                "nullable": False,
                "classification": "non-personal",
            },
            {
                "name": "opened_date",
                "type": "date",
                "nullable": False,
                "classification": "non-personal",
            },
            {
                "name": "updated_at",
                "type": "timestamp with time zone",
                "nullable": False,
                "classification": "non-personal",
            },
        ],
    },
    "test",
)

NOW = dt.datetime(2026, 9, 19, 2, 0, tzinfo=dt.UTC)


def record(**overrides):
    base = {
        "account_id": 1,
        "iban": "DE49500105175407324931",
        "current_balance_amount": Decimal("100.0000"),
        "is_deleted": False,
        "opened_date": dt.date(2026, 3, 1),
        "updated_at": NOW,
    }
    base.update(overrides)
    return base


# --- types -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "declared"),
    [
        ("text", "character varying(34)"),
        ("text", "text"),
        ("DE", "character(2)"),
        (1, "bigint"),
        (1, "smallint"),
        (Decimal("1.5"), "numeric(18,4)"),
        (1, "numeric(18,4)"),
        (True, "boolean"),
        (dt.date(2026, 1, 1), "date"),
        (NOW, "timestamp with time zone"),
    ],
)
def test_a_well_typed_value_matches(value, declared):
    assert matches_type(value, declared)


@pytest.mark.parametrize(
    ("value", "declared"),
    [
        (1, "character varying(34)"),
        ("1", "bigint"),
        (True, "bigint"),
        (True, "numeric(18,4)"),
        ("true", "boolean"),
        (NOW, "date"),
        (dt.date(2026, 1, 1), "timestamp with time zone"),
    ],
)
def test_a_badly_typed_value_does_not_match(value, declared):
    assert not matches_type(value, declared)


def test_length_is_not_a_gate():
    """The declared width describes the source column, not the token that replaces it."""
    assert matches_type("x" * 400, "character varying(34)")


def test_a_type_the_gate_does_not_understand_is_an_error_rather_than_a_pass():
    with pytest.raises(ValueError, match="does not understand"):
        matches_type("anything", "geography(point)")


# --- per-record validation -------------------------------------------------------------


def test_a_well_formed_record_is_accepted():
    assert validate_record(record(), CONTRACT, set()) is None


def test_a_null_primary_key_is_rejected():
    rejection = validate_record(record(account_id=None), CONTRACT, set())
    assert rejection.reason == QuarantineReason.PRIMARY_KEY_NULL
    assert rejection.column == "account_id"


def test_a_duplicate_primary_key_within_the_batch_is_rejected():
    rejection = validate_record(record(), CONTRACT, {1})
    assert rejection.reason == QuarantineReason.PRIMARY_KEY_DUPLICATE
    assert rejection.record_key == 1


def test_a_null_in_a_non_nullable_column_is_rejected():
    rejection = validate_record(record(is_deleted=None), CONTRACT, set())
    assert rejection.reason == QuarantineReason.NULL_IN_NON_NULLABLE
    assert rejection.column == "is_deleted"


def test_a_null_in_a_nullable_column_is_accepted():
    assert validate_record(record(iban=None), CONTRACT, set()) is None


def test_a_type_mismatch_is_rejected_with_the_offending_value():
    rejection = validate_record(record(current_balance_amount="lots"), CONTRACT, set())
    assert rejection.reason == QuarantineReason.TYPE_MISMATCH
    assert rejection.column == "current_balance_amount"
    assert rejection.value == "lots"


def test_a_column_the_contract_declares_and_the_record_lacks_is_rejected():
    body = record()
    del body["opened_date"]
    rejection = validate_record(body, CONTRACT, set())
    assert rejection.reason == QuarantineReason.MISSING_COLUMN


def test_validation_reports_the_first_failure_in_contract_order():
    rejection = validate_record(record(iban=7, is_deleted=None), CONTRACT, set())
    assert rejection.column == "iban"


def test_landed_plus_quarantined_equals_read():
    result = validate(
        [record(account_id=1), record(account_id=2, is_deleted=None), record(account_id=1)],
        CONTRACT,
    )
    assert result.rows_landed == 1
    assert result.rows_quarantined == 2
    assert result.rows_read == 3


def test_a_rejected_record_does_not_reserve_its_key():
    """A record refused for a later column must not block a good record with the same key.

    It cannot arise from this source, where keys are unique, but the alternative behaviour
    would silently quarantine a valid retry of a corrected record.
    """
    result = validate([record(account_id=1, is_deleted=None), record(account_id=1)], CONTRACT)
    assert result.rows_landed == 1
    assert result.rows_quarantined == 1


# --- projection ------------------------------------------------------------------------


def test_projection_drops_columns_the_contract_does_not_describe():
    body = record(merchant_risk_score=Decimal("0.5"))
    projected = project(body, CONTRACT.column_names)
    assert "merchant_risk_score" not in projected
    assert tuple(projected) == CONTRACT.column_names


# --- drift ------------------------------------------------------------------------------


def live(**overrides) -> dict[str, str]:
    types = {column.name: column.data_type for column in CONTRACT.columns}
    types.update(overrides)
    return types


def test_no_drift_when_the_source_matches_the_contract():
    report = classify(CONTRACT, live(), "account_id")
    assert report.observations == ()
    assert not report.is_breaking
    assert report.summary() == "no drift"


def test_an_added_column_is_additive_and_not_breaking():
    report = classify(CONTRACT, live(merchant_risk_score="numeric(18,8)"), "account_id")
    assert [o.kind for o in report.observations] == [ADDITIVE]
    assert not report.is_breaking
    assert report.additive[0].column == "merchant_risk_score"


def test_a_widened_type_is_breaking():
    report = classify(CONTRACT, live(iban="character varying(280)"), "account_id")
    assert [o.kind for o in report.observations] == [TYPE_CHANGED]
    assert report.is_breaking


def test_a_removed_column_is_breaking():
    types = live()
    del types["iban"]
    report = classify(CONTRACT, types, "account_id")
    assert [o.kind for o in report.observations] == [COLUMN_REMOVED]
    assert report.is_breaking


def test_a_changed_primary_key_is_breaking():
    report = classify(CONTRACT, live(), "iban")
    assert [o.kind for o in report.observations] == [PRIMARY_KEY_CHANGED]
    assert report.is_breaking


def test_an_unknown_primary_key_is_not_treated_as_a_change():
    """A caller that could not read the key set says so with None rather than guessing."""
    assert classify(CONTRACT, live(), None).observations == ()


def test_additive_and_breaking_together_are_breaking():
    types = live(merchant_risk_score="numeric(18,8)", iban="text")
    report = classify(CONTRACT, types, "account_id")
    assert report.is_breaking
    assert len(report.additive) == 1
    assert len(report.breaking) == 1


def test_the_action_recorded_follows_the_kind():
    additive = classify(CONTRACT, live(extra="text"), "account_id").observations[0]
    breaking = classify(CONTRACT, live(iban="text"), "account_id").observations[0]
    assert "omitted from bronze" in additive.action
    assert "watermark unmoved" in breaking.action
