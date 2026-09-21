"""Unit tests for the register step: the vault, the quarantine index and the reconciliation.

Against a real in-memory DuckDB, because all three are SQL and a fake would test the fake.

Two of these exist because running the real thing found the defect first. The vault insert
went through one parameter list per value and exhausted DuckDB's memory limit inside the
register transaction at the initial load; the reconciliation's per-day count read every bronze
object back to recompute a number the extract phase already had. Both are fixed and both are
pinned here.

No Airflow and no stack: these run in `make test`.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb
import pytest
from nordbank_ops.register import landed_on_day, upsert_vault, write_reconciliation
from nordbank_ops.tokenise import Tokeniser

SCHEMA_DIR = Path(__file__).resolve().parent.parent.parent / "infra" / "warehouse" / "schema"
NOW = dt.datetime(2026, 9, 21, 4, 0, tzinfo=dt.UTC)
SOURCE_DATE = dt.date(2026, 9, 19)


@pytest.fixture
def connection():
    con = duckdb.connect(":memory:")
    for path in sorted(SCHEMA_DIR.glob("*.sql")):
        code = "\n".join(
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if not line.strip().startswith("--")
        )
        for statement in code.split(";"):
            if statement.strip():
                con.execute(statement)
    yield con
    con.close()


def tokeniser() -> Tokeniser:
    return Tokeniser.from_environment({"PII_TOKEN_SALT": "a-test-salt"})


def vault(connection, values, **overrides):
    arguments = {
        "source_system": "corebank",
        "entity": "customers",
        "column": "full_name",
        "batch_id": "customers-20260919T000000-01",
        "now": NOW,
    }
    arguments.update(overrides)
    return upsert_vault(connection, tokeniser(), values, **arguments)


# --- the vault ----------------------------------------------------------------------------


def test_the_vault_holds_one_row_per_distinct_value(connection):
    added = vault(connection, ["Jan Kowalski", "Ada Nowak", "Jan Kowalski"])
    assert added == 2
    assert connection.execute("select count(*) from meta.pii_vault").fetchone()[0] == 2


def test_re_extraction_adds_nothing(connection):
    vault(connection, ["Jan Kowalski"])
    assert vault(connection, ["Jan Kowalski"]) == 0
    assert connection.execute("select count(*) from meta.pii_vault").fetchone()[0] == 1


def test_the_same_value_in_a_second_entity_does_not_create_a_second_row(connection):
    """949 payments in the `ci` book pay a Nordbank customer by name. One person, one row."""
    vault(connection, ["Jan Kowalski"])
    added = vault(connection, ["Jan Kowalski"], entity="payments", column="counterparty_name")
    assert added == 0
    row = connection.execute(
        "select first_seen_entity, first_seen_column from meta.pii_vault"
    ).fetchone()
    assert row == ("customers", "full_name")


def test_the_vault_is_keyed_on_the_token(connection):
    vault(connection, ["Jan Kowalski"])
    token = tokeniser().token("Jan Kowalski")
    stored = connection.execute(
        "select token, raw_value from meta.pii_vault where token = ?", [token]
    ).fetchone()
    assert stored == (token, "Jan Kowalski")


def test_an_empty_batch_writes_no_vault_row(connection):
    assert vault(connection, []) == 0


def test_a_value_repeated_within_one_batch_does_not_conflict_with_itself(connection):
    """The insert deduplicates before it reaches the table, so one statement is enough."""
    assert vault(connection, ["Jan Kowalski"] * 500) == 1


# --- the reconciliation ---------------------------------------------------------------------


def test_the_per_day_count_comes_from_the_extract_report():
    report = {"landed_by_source_date": {"2026-09-19": 34, "2026-09-18": 7}}
    assert landed_on_day(report, SOURCE_DATE) == 34
    assert landed_on_day(report, dt.date(2026, 9, 20)) == 0


def test_a_report_with_no_per_day_counts_reconciles_to_zero():
    assert landed_on_day({}, SOURCE_DATE) == 0


def test_the_difference_is_landed_plus_quarantined_against_the_claim(connection):
    write_reconciliation(
        connection,
        source_system="corebank",
        entity="accounts",
        source_date=SOURCE_DATE,
        rows_claimed=210,
        rows_landed=209,
        rows_quarantined=1,
        batch_id="accounts-20260919T000000-01",
        now=NOW,
    )
    assert connection.execute("select difference from ops.source_reconciliation").fetchone() == (0,)


def test_no_claim_records_a_null_difference_rather_than_a_zero(connection):
    """ "Nothing changed" and "nothing claimed" are different facts."""
    write_reconciliation(
        connection,
        source_system="corebank",
        entity="accounts",
        source_date=SOURCE_DATE,
        rows_claimed=None,
        rows_landed=598,
        rows_quarantined=0,
        batch_id="accounts-20260918T000000-01",
        now=NOW,
    )
    assert connection.execute("select difference from ops.source_reconciliation").fetchone() == (
        None,
    )


def test_a_re_run_replaces_the_reconciliation_row_rather_than_doubling_it(connection):
    for landed, batch_id in ((100, "a-01"), (210, "a-02")):
        write_reconciliation(
            connection,
            source_system="corebank",
            entity="accounts",
            source_date=SOURCE_DATE,
            rows_claimed=210,
            rows_landed=landed,
            rows_quarantined=0,
            batch_id=batch_id,
            now=NOW,
        )
    rows = connection.execute(
        "select rows_landed, batch_id, difference from ops.source_reconciliation"
    ).fetchall()
    assert rows == [(210, "a-02", 0)]
