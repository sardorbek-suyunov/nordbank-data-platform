"""The live source schema against the committed data dictionary, inside the running stack.

This is the control that stops the dictionary rotting (spec 002 section 7). `make schema-check`
runs the same comparison from the host through psql; this runs it through a driver as the
extraction role, which also proves the reader can see everything it needs to.

Alongside the comparison it asserts the schema-wide invariants that are cheap to check from the
catalogue and expensive to notice by hand: the audit columns by schema, the updated_at index
and trigger on every table, the absence of floating point, the absence of anywhere a card
number could be held, and an index on every foreign key.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from schema_contract import compare, live_columns, read_dictionary

pytestmark = pytest.mark.integration

SCHEMAS = ("core", "ref", "platform")

# The tests run from the repository and from /opt/airflow inside the project image.
DICTIONARY_CANDIDATES = (
    Path("/opt/airflow/docs/data_dictionary.md"),
    Path(__file__).resolve().parents[2] / "docs" / "data_dictionary.md",
)


def _dictionary_path() -> Path:
    for candidate in DICTIONARY_CANDIDATES:
        if candidate.is_file():
            return candidate
    pytest.skip(f"data dictionary not found at any of {[str(p) for p in DICTIONARY_CANDIDATES]}")


@pytest.fixture(scope="module")
def connection():
    psycopg2 = pytest.importorskip("psycopg2")
    dsn = os.environ.get("AIRFLOW_CONN_NORDBANK_SOURCE_DB")
    if not dsn:
        pytest.skip("AIRFLOW_CONN_NORDBANK_SOURCE_DB is not set")
    try:
        conn = psycopg2.connect(dsn, connect_timeout=5)
    except psycopg2.OperationalError as exc:
        pytest.skip(f"source database unreachable: {exc}")
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def execute(connection):
    def run(sql: str) -> list[tuple]:
        with connection.cursor() as cursor:
            cursor.execute(sql)
            return [tuple("" if value is None else str(value) for value in row) for row in cursor]

    return run


@pytest.fixture(scope="module")
def documented():
    return read_dictionary(_dictionary_path())


def test_the_schema_has_not_been_applied_is_reported_not_silently_passed(execute) -> None:
    rows = execute(
        "select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace "
        f"where c.relkind = 'r' and n.nspname in {SCHEMAS}"
    )
    if int(rows[0][0]) == 0:
        pytest.fail("no tables in core, ref or platform: run `make schema-apply` first")


def test_live_schema_matches_the_data_dictionary(documented, execute) -> None:
    differences = compare(documented, live_columns(execute), execute)

    assert not differences, "\n".join(str(difference) for difference in differences)


def test_every_column_is_classified_and_nothing_extra_is(documented, execute) -> None:
    rows = execute(
        "select schema_name, table_name, column_name from platform.column_classifications"
    )
    classified = {tuple(row) for row in rows}
    live = {(column.schema, column.table, column.name) for column in live_columns(execute)}

    assert not live - classified, f"unclassified columns: {sorted(live - classified)}"
    assert not classified - live, f"classified but absent: {sorted(classified - live)}"


def test_classifications_use_only_the_five_classes(execute) -> None:
    rows = execute("select distinct classification from platform.column_classifications")

    assert {row[0] for row in rows} <= {
        "identifier",
        "quasi-identifier",
        "pseudonymous_key",
        "sensitive",
        "non-personal",
    }


def test_every_join_path_to_a_person_is_a_pseudonymous_key(execute) -> None:
    """No key that resolves to a person may be classified non-personal.

    The first pass classified all of them that way, which was wrong: an internal customer
    number is pseudonymised personal data. It cannot be tokenised, because it is the pseudonym
    rather than the identifier and tokenising it would break every join, so the class exists to
    say that it is personal without asking for it to be transformed.
    """
    rows = execute(
        """
        with person_bearing (t) as (
            values ('customers'), ('customer_addresses'), ('accounts'), ('account_holders'),
                   ('cards'), ('loans'), ('loan_applications')
        ),
        keys as (
            select c.relname as tbl, a.attname as col
              from pg_constraint con
              join pg_class c      on c.oid = con.conrelid
              join pg_namespace n  on n.oid = c.relnamespace
              join unnest(con.conkey) k(attnum) on true
              join pg_attribute a  on a.attrelid = con.conrelid and a.attnum = k.attnum
             where n.nspname = 'core'
               and (
                   (con.contype = 'p' and c.relname in (select t from person_bearing))
                or (con.contype = 'f' and con.confrelid in (
                        select fc.oid from pg_class fc
                          join pg_namespace fn on fn.oid = fc.relnamespace
                         where fn.nspname = 'core'
                           and fc.relname in (select t from person_bearing)))
               )
        )
        select 'core.' || k.tbl || '.' || k.col, cc.classification
          from keys k
          join platform.column_classifications cc
            on cc.schema_name = 'core' and cc.table_name = k.tbl and cc.column_name = k.col
         where cc.classification <> 'pseudonymous_key'
         order by 1
        """
    )

    assert rows == [], f"join paths to a person not classified as pseudonymous_key: {rows}"


def test_every_table_carries_its_audit_columns(execute) -> None:
    rows = execute(
        f"""
        select n.nspname, c.relname,
               bool_or(a.attname = 'created_at'),
               bool_or(a.attname = 'updated_at'),
               bool_or(a.attname = 'is_deleted'),
               bool_or(a.attname = 'is_active')
          from pg_class c
          join pg_namespace n on n.oid = c.relnamespace
          join pg_attribute a on a.attrelid = c.oid and a.attnum > 0 and not a.attisdropped
         where c.relkind = 'r' and n.nspname in {SCHEMAS}
         group by 1, 2
        """
    )
    assert rows

    for schema, table, created, updated, deleted, active in rows:
        where = f"{schema}.{table}"
        assert created == "True", f"{where} has no created_at"
        assert updated == "True", f"{where} has no updated_at"
        if schema == "core":
            assert deleted == "True", f"{where} has no is_deleted"
        if schema == "ref":
            assert active == "True", f"{where} has no is_active"
            # Reference rows are deactivated, never deleted. Two overlapping flags would be a
            # defect rather than thoroughness.
            assert deleted == "False", f"{where} carries is_deleted"


def test_every_table_has_an_updated_at_index_and_the_trigger(execute) -> None:
    tables = execute(
        f"""
        select n.nspname || '.' || c.relname
          from pg_class c join pg_namespace n on n.oid = c.relnamespace
         where c.relkind = 'r' and n.nspname in {SCHEMAS}
        """
    )
    indexed = execute(
        f"""
        select n.nspname || '.' || t.relname
          from pg_index x
          join pg_class t     on t.oid = x.indrelid
          join pg_namespace n on n.oid = t.relnamespace
          join pg_attribute a on a.attrelid = t.oid
                             and a.attnum = (string_to_array(x.indkey::text, ' ')::smallint[])[1]
         where n.nspname in {SCHEMAS} and a.attname = 'updated_at'
        """
    )
    triggered = execute(
        f"""
        select n.nspname || '.' || c.relname
          from pg_trigger tg
          join pg_class c     on c.oid = tg.tgrelid
          join pg_namespace n on n.oid = c.relnamespace
         where n.nspname in {SCHEMAS}
           and tg.tgname = 'set_updated_at'
           and not tg.tgisinternal
           and tg.tgenabled <> 'D'
        """
    )

    all_tables = {row[0] for row in tables}
    assert not all_tables - {row[0] for row in indexed}, "tables without an updated_at index"
    assert not all_tables - {row[0] for row in triggered}, "tables without the trigger"


def test_no_floating_point_column_exists(execute) -> None:
    rows = execute(
        f"""
        select n.nspname || '.' || c.relname || '.' || a.attname, t.typname
          from pg_attribute a
          join pg_class c     on c.oid = a.attrelid
          join pg_namespace n on n.oid = c.relnamespace
          join pg_type t      on t.oid = a.atttypid
         where n.nspname in {SCHEMAS} and c.relkind = 'r'
           and a.attnum > 0 and not a.attisdropped
           and t.typname in ('float4', 'float8')
        """
    )

    assert rows == [], f"floating point columns: {rows}"


def test_no_column_is_named_like_a_card_number(execute) -> None:
    rows = execute(
        f"""
        select n.nspname || '.' || c.relname || '.' || a.attname
          from pg_attribute a
          join pg_class c     on c.oid = a.attrelid
          join pg_namespace n on n.oid = c.relnamespace
         where n.nspname in {SCHEMAS} and c.relkind = 'r'
           and a.attnum > 0 and not a.attisdropped
           and a.attname in ('pan', 'card_number', 'primary_account_number')
        """
    )

    assert rows == []


def test_no_character_column_on_cards_can_hold_a_card_number(execute) -> None:
    """A card number is thirteen to nineteen digits. Every character column here is under that.

    Acceptance criterion 4 as literally worded is unprovable, because any wide text column
    could hold a card number. This is the bar instead.
    """
    rows = execute(
        """
        select a.attname, format_type(a.atttypid, a.atttypmod), a.atttypmod - 4
          from pg_attribute a
          join pg_class c     on c.oid = a.attrelid
          join pg_namespace n on n.oid = c.relnamespace
          join pg_type t      on t.oid = a.atttypid
         where n.nspname = 'core' and c.relname = 'cards'
           and a.attnum > 0 and not a.attisdropped
           and t.typname in ('text', 'varchar', 'bpchar')
        """
    )
    assert rows, "no character columns found on core.cards"

    for name, declared, length in rows:
        assert int(length) > 0, f"core.cards.{name} is unbounded ({declared})"
        assert int(length) < 13, f"core.cards.{name} admits {length} characters ({declared})"


def test_the_card_digit_columns_are_fixed_length_and_digits_only(execute) -> None:
    types = dict(
        execute(
            """
            select a.attname, format_type(a.atttypid, a.atttypmod)
              from pg_attribute a
              join pg_class c     on c.oid = a.attrelid
              join pg_namespace n on n.oid = c.relnamespace
             where n.nspname = 'core' and c.relname = 'cards'
               and a.attname in ('card_bin', 'card_last_four')
            """
        )
    )
    assert types == {"card_bin": "character(6)", "card_last_four": "character(4)"}

    checks = {
        row[0]: row[1]
        for row in execute(
            """
            select con.conname, pg_get_constraintdef(con.oid)
              from pg_constraint con
              join pg_class c     on c.oid = con.conrelid
              join pg_namespace n on n.oid = c.relnamespace
             where n.nspname = 'core' and c.relname = 'cards' and con.contype = 'c'
            """
        )
    }
    assert "[0-9]{6}" in checks["cards_bin_ck"]
    assert "[0-9]{4}" in checks["cards_last_four_ck"]


def test_every_foreign_key_column_is_indexed(execute) -> None:
    rows = execute(
        f"""
        select n.nspname || '.' || t.relname || ' ' || con.conname
          from pg_constraint con
          join pg_class t     on t.oid = con.conrelid
          join pg_namespace n on n.oid = t.relnamespace
         where con.contype = 'f' and n.nspname in {SCHEMAS}
           and not exists (
               select 1 from pg_index i
                where i.indrelid = con.conrelid
                  and (string_to_array(i.indkey::text, ' ')::smallint[])
                      [1:array_length(con.conkey, 1)] = con.conkey
           )
        """
    )

    assert rows == [], f"foreign keys without an index: {rows}"


def test_the_entry_side_sign_multiplier_agrees_with_the_ledger_check_constraint(execute) -> None:
    """The same fact is held in two places, so the two are asserted to agree.

    A check constraint cannot read another table, so core.gl_entries enforces the sign row by
    row while ref.entry_sides carries the multiplier downstream models use. The seed makes them
    agree; this makes sure it stayed that way.
    """
    sides = dict(execute("select code, sign_multiplier from ref.entry_sides"))
    assert sides == {"D": "1", "C": "-1"}

    constraint = execute(
        """
        select pg_get_constraintdef(con.oid)
          from pg_constraint con
          join pg_class c     on c.oid = con.conrelid
          join pg_namespace n on n.oid = c.relnamespace
         where n.nspname = 'core' and c.relname = 'gl_entries'
           and con.conname = 'gl_entries_sign_matches_side_ck'
        """
    )
    assert constraint, "the sign constraint is missing from core.gl_entries"
    assert "'D'" in constraint[0][0] and "amount > " in constraint[0][0]


def test_the_balance_trigger_is_deferred(execute) -> None:
    rows = execute(
        """
        select tg.tgdeferrable, tg.tginitdeferred, tg.tgconstraint <> 0
          from pg_trigger tg
          join pg_class c     on c.oid = tg.tgrelid
          join pg_namespace n on n.oid = c.relnamespace
         where n.nspname = 'core' and c.relname = 'gl_entries'
           and tg.tgname = 'gl_entries_balanced'
        """
    )

    assert rows, "the gl_entries balance trigger is missing"
    deferrable, initially_deferred, is_constraint_trigger = rows[0]
    assert is_constraint_trigger == "True"
    assert deferrable == "True"
    assert initially_deferred == "True"


def test_the_reader_can_select_from_every_table(execute) -> None:
    """Including platform.column_classifications, which is no more restricted than any other."""
    rows = execute(
        f"""
        select n.nspname || '.' || c.relname
          from pg_class c
          join pg_namespace n on n.oid = c.relnamespace
         where c.relkind = 'r' and n.nspname in {SCHEMAS}
           and not has_table_privilege(current_user, c.oid, 'select')
        """
    )

    assert rows == [], f"tables the extraction role cannot read: {rows}"
