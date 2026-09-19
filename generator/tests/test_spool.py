"""CSV formatting for `COPY`.

The one thing that has to be right here is that NULL and the empty string stay different.
`COPY ... WITH (FORMAT CSV)` reads an unquoted empty field as NULL and a quoted one as an empty
string, so a writer that quoted everything would turn every absent value into an empty string —
which fails a not-null constraint where there is one and, where there is not, silently stores
the wrong thing.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from generator.spool import Spool, format_row, format_value
from generator.tables import LOAD_ORDER, TABLE_COLUMNS


def test_none_is_null_and_empty_string_is_not():
    assert format_value(None) == ""
    assert format_value("") == '""'


def test_booleans_use_the_postgres_literals():
    assert format_value(True) == "t"
    assert format_value(False) == "f"


def test_decimals_keep_their_scale():
    assert format_value(Decimal("10.5000")) == "10.5000"
    assert format_value(Decimal("-0.0001")) == "-0.0001"


def test_timestamps_are_iso_and_carry_their_offset():
    moment = dt.datetime(2026, 3, 18, 9, 15, 30, tzinfo=dt.UTC)
    assert format_value(moment) == "2026-03-18 09:15:30+00:00"
    assert format_value(dt.date(2026, 3, 18)) == "2026-03-18"


def test_values_that_would_break_the_row_are_quoted():
    assert format_value("Alpha, Beta") == '"Alpha, Beta"'
    assert format_value('He said "hi"') == '"He said ""hi"""'
    assert format_value("two\nlines") == '"two\nlines"'


def test_ordinary_values_are_not_quoted():
    assert format_value("Bluebird Retail") == "Bluebird Retail"


def test_a_row_is_comma_separated_and_newline_terminated():
    assert format_row([1, None, "x", True]) == "1,,x,t\n"


def test_writing_the_wrong_number_of_values_is_refused(tmp_path: Path):
    spool = Spool(tmp_path)
    table = spool.table("merchants")
    with pytest.raises(ValueError, match="values for"):
        table.write((1, 2))
    spool.close()


def test_every_core_table_has_columns_and_a_place_in_the_load_order():
    assert set(LOAD_ORDER) == set(TABLE_COLUMNS)
    assert len(LOAD_ORDER) == 16
    for table, columns in TABLE_COLUMNS.items():
        assert columns[0].endswith("_id"), f"{table} does not start with its identity column"
        for audit in ("created_at", "updated_at", "is_deleted"):
            assert audit in columns, f"{table} is missing {audit}"


def test_parents_come_before_children_in_the_load_order():
    position = {table: index for index, table in enumerate(LOAD_ORDER)}
    dependencies = {
        "customer_addresses": ["customers"],
        "accounts": ["customers"],
        "account_holders": ["accounts", "customers"],
        "cards": ["accounts"],
        "loan_applications": ["customers"],
        "loans": ["loan_applications", "customers"],
        "loan_installments": ["loans"],
        "transactions": ["accounts", "cards", "merchants", "agent_locations"],
        "payments": ["accounts"],
        "gl_entries": ["gl_transactions"],
        "fraud_alerts": ["transactions", "customers"],
        "login_sessions": ["customers"],
    }
    for child, parents in dependencies.items():
        for parent in parents:
            assert position[parent] < position[child], f"{parent} must load before {child}"
