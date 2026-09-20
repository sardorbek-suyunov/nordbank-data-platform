"""Spec 002 design rule 4 as amended: the updated_at trigger reads a simulation clock.

Every assertion runs inside a transaction that rolls back, so these leave the loaded book
exactly as they found it. What they are protecting is the property the whole milestone rests
on: a tick simulates a past date, and an UPDATE that stamped the wall clock would hand the
entire mutated source to M4 inside one extraction window.

Three of these look redundant and are not.

The empty-string case is the reason `nullif` is in the trigger. A custom setting that was never
set reads as NULL, but one set with SET LOCAL reads as `''` for the rest of the session once
its transaction ends, so a trigger written with `coalesce` alone would raise on the second tick
over a reused connection rather than on the first.

The clearing case is the reason `platform` rows carry real time: the trigger is attached to all
three schemas, so a tick that left the clock set would backdate its own reconciliation control.

The leak case is what makes SET LOCAL rather than SET the right instrument, and it is worth a
test because the difference is one word and the failure is silent.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from source_db_driver import (  # noqa: E402
    clear_simulation_clock,
    connect,
    set_simulation_clock,
)

pytestmark = pytest.mark.integration

SIM = dt.datetime(2024, 3, 15, 9, 0, tzinfo=dt.UTC)

# A ref row rather than a core one: ref is seeded by schema-apply and is not part of any
# profile's row counts, so a rolled-back update to it cannot perturb a manifest comparison.
TARGET = "update ref.channels set name = name where code = 'web'"
READ = "select updated_at from ref.channels where code = 'web'"


@pytest.fixture
def cursor():
    with connect() as conn:
        with conn.cursor() as cur:
            yield cur
        conn.rollback()


def test_with_no_clock_set_the_trigger_stamps_the_wall_clock(cursor):
    cursor.execute(TARGET)
    cursor.execute(READ)
    stamped = cursor.fetchone()[0]
    cursor.execute("select now()")
    assert stamped == cursor.fetchone()[0]


def test_with_the_clock_set_the_trigger_stamps_the_simulated_instant(cursor):
    set_simulation_clock(cursor, SIM)
    cursor.execute(TARGET)
    cursor.execute(READ)
    assert cursor.fetchone()[0] == SIM


def test_the_clock_survives_unrelated_writes_in_the_same_transaction(cursor):
    set_simulation_clock(cursor, SIM)
    cursor.execute("update ref.currencies set name = name where code = 'EUR'")
    cursor.execute(TARGET)
    cursor.execute(READ)
    assert cursor.fetchone()[0] == SIM


def test_clearing_the_clock_returns_the_trigger_to_the_wall_clock(cursor):
    # The ordering rule platform rows depend on: set once, write core and ref, clear once,
    # write platform last.
    set_simulation_clock(cursor, SIM)
    cursor.execute(TARGET)
    cursor.execute(READ)
    assert cursor.fetchone()[0] == SIM

    clear_simulation_clock(cursor)
    cursor.execute(TARGET)
    cursor.execute(READ)
    cleared = cursor.fetchone()[0]
    cursor.execute("select now()")
    assert cleared == cursor.fetchone()[0]


def test_an_unset_clock_reads_null_and_a_cleared_one_reads_empty(cursor):
    cursor.execute("select current_setting('nordbank.unset_probe', true)")
    assert cursor.fetchone()[0] is None

    clear_simulation_clock(cursor)
    cursor.execute("select current_setting('nordbank.sim_now', true)")
    assert cursor.fetchone()[0] == ""

    # Which is exactly what the trigger's nullif turns back into a fallback.
    cursor.execute(
        "select coalesce(nullif(current_setting('nordbank.sim_now', true), '')::timestamptz, "
        "now()) = now()"
    )
    assert cursor.fetchone()[0] is True


def test_the_clock_does_not_leak_into_another_session():
    with connect() as holder, connect() as observer:
        with holder.cursor() as held:
            set_simulation_clock(held, SIM)
            held.execute("select current_setting('nordbank.sim_now', true)")
            assert held.fetchone()[0] is not None

            with observer.cursor() as seen:
                seen.execute("select current_setting('nordbank.sim_now', true)")
                assert seen.fetchone()[0] in (None, "")
        holder.rollback()


def test_the_clock_does_not_survive_its_own_transaction():
    with connect() as conn:
        with conn.cursor() as cur:
            set_simulation_clock(cur, SIM)
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(TARGET)
            cur.execute(READ)
            stamped = cur.fetchone()[0]
            cur.execute("select now()")
            assert stamped == cur.fetchone()[0]
        conn.rollback()
