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


def daily(connection):
    return connection.execute(
        "select rows_claimed, rows_landed, rows_quarantined, difference, batches "
        "from ops.source_reconciliation_daily"
    ).fetchone()


def test_one_batch_is_one_row(connection):
    reconcile(connection, 210, "accounts-01")
    assert connection.execute("select count(*) from ops.source_reconciliation").fetchone() == (1,)
    assert daily(connection) == (210, 210, 0, 0, 1)


def test_a_retry_of_the_same_batch_replaces_its_own_contribution(connection):
    reconcile(connection, 100, "accounts-01")
    reconcile(connection, 210, "accounts-01")
    assert connection.execute("select count(*) from ops.source_reconciliation").fetchone() == (1,)
    assert daily(connection) == (210, 210, 0, 0, 1)


def test_two_batches_covering_one_day_sum_rather_than_fight(connection):
    """The drift day, and the reason the table is keyed per batch.

    Keyed per day and upserted, the narrow re-run after a halt overwrote the wide batch's
    count and the control reported a discrepancy only its own bookkeeping had created. Keyed
    per batch, the day is the sum: the batch before the halt landed most of it and the batch
    after the contract bump landed the rest.
    """
    reconcile(connection, 267, "accounts-01")
    reconcile(connection, 4, "accounts-02", claimed=271)
    assert connection.execute("select count(*) from ops.source_reconciliation").fetchone() == (2,)
    assert daily(connection) == (271, 271, 0, 0, 2)


def test_an_unclaimed_day_keeps_a_null_difference(connection):
    """ "Nothing changed" and "nothing claimed" are different facts."""
    reconcile(connection, 598, "accounts-01", claimed=None)
    assert daily(connection) == (None, 598, 0, None, 1)


def test_quarantined_rows_count_towards_the_day(connection):
    reconcile(connection, 209, "accounts-01", claimed=210, quarantined=1)
    assert daily(connection) == (210, 209, 1, 0, 1)


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
