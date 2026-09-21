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


def reconcile(connection, landed, batch_id, claimed=210, quarantined=0):
    write_reconciliation(
        connection,
        source_system="corebank",
        entity="accounts",
        source_date=SOURCE_DATE,
        rows_claimed=claimed,
        rows_landed=landed,
        rows_quarantined=quarantined,
        batch_id=batch_id,
        now=NOW,
    )


def test_a_re_run_does_not_double_the_reconciliation_row(connection):
    reconcile(connection, 100, "a-01")
    reconcile(connection, 210, "a-02")
    rows = connection.execute("select count(*) from ops.source_reconciliation").fetchall()
    assert rows == [(1,)]


def test_a_narrower_re_run_does_not_replace_the_day_count(connection):
    """The defect the first sixty-day backfill produced, and the reason for `greatest`.

    A re-run after registration reads from the watermark the first batch advanced, so its
    window is a tail of the day rather than the day. Overwriting with its count made nine
    entities on the drift day report a handful of rows against a claim of hundreds.
    """
    reconcile(connection, 271, "accounts-01")
    reconcile(connection, 4, "accounts-02")
    row = connection.execute(
        "select rows_landed, batch_id, difference from ops.source_reconciliation"
    ).fetchone()
    assert row == (271, "accounts-01", 61)


def test_a_wider_re_run_does_replace_it(connection):
    reconcile(connection, 4, "accounts-01")
    reconcile(connection, 271, "accounts-02")
    row = connection.execute(
        "select rows_landed, batch_id from ops.source_reconciliation"
    ).fetchone()
    assert row == (271, "accounts-02")


def test_the_difference_is_recomputed_from_the_surviving_counts(connection):
    reconcile(connection, 210, "accounts-01")
    reconcile(connection, 2, "accounts-02")
    assert connection.execute("select difference from ops.source_reconciliation").fetchone() == (0,)


# --- the source's claim --------------------------------------------------------------------


class FakeSourceCursor:
    """Answers the two queries `claimed_by_source` issues."""

    def __init__(self, ticks: set[dt.date], logged: dict[tuple[dt.date, str], int]):
        self.ticks = ticks
        self.logged = logged
        self._result = None

    def execute(self, sql, parameters):
        if "count(*) from platform.tick_log" in " ".join(sql.split()):
            self._result = (1 if parameters[0] in self.ticks else 0,)
        else:
            self._result = (self.logged.get((parameters[0], parameters[1]), 0),)

    def fetchone(self):
        return self._result


class FakeContract:
    def __init__(self, entity: str, source_schema: str):
        self.entity = entity
        self.source_schema = source_schema


def test_no_tick_that_day_means_no_claim():
    """The initial load of the historical book. Nothing claimed is not nothing changed."""
    from nordbank_ops.register import claimed_by_source

    cursor = FakeSourceCursor(ticks=set(), logged={})
    assert claimed_by_source(cursor, FakeContract("accounts", "core"), SOURCE_DATE) is None


def test_a_reference_entity_is_never_claimed():
    """The tick engine covers the sixteen core tables and never touches `ref`.

    Reading its silence as a claim of zero made twenty-nine reference entities look like a
    441-row discrepancy on the first day of the first backfill.
    """
    from nordbank_ops.register import claimed_by_source

    cursor = FakeSourceCursor(ticks={SOURCE_DATE}, logged={})
    assert claimed_by_source(cursor, FakeContract("currencies", "ref"), SOURCE_DATE) is None


def test_a_core_entity_the_tick_did_not_log_is_claimed_as_zero():
    """The tick writes a row only for the tables it touched, so silence is a claim of zero."""
    from nordbank_ops.register import claimed_by_source

    cursor = FakeSourceCursor(ticks={SOURCE_DATE}, logged={})
    assert claimed_by_source(cursor, FakeContract("agent_locations", "core"), SOURCE_DATE) == 0


def test_a_core_entity_the_tick_logged_is_claimed_at_its_count():
    from nordbank_ops.register import claimed_by_source

    cursor = FakeSourceCursor(ticks={SOURCE_DATE}, logged={(SOURCE_DATE, "accounts"): 210})
    assert claimed_by_source(cursor, FakeContract("accounts", "core"), SOURCE_DATE) == 210
