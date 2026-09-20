"""The tick state machine and its refusals, spec 004 acceptance criterion 1.

No database: `require` only needs something that answers a select, so a fake cursor is enough
and these run in `make test`. What is being pinned is not that a refusal happens but that it
names the date the machine expected, because that is what the criterion asks for and it is the
difference between an error a reader can act on and one they cannot.
"""

from __future__ import annotations

import datetime as dt

import pytest

from generator.mutation.state import SimulationState, TickRefusedError, require

ANCHOR = dt.date(2026, 9, 18)


class FakeCursor:
    """Answers the one select `require` issues, and records that it asked for a lock."""

    def __init__(self, row):
        self._row = row
        self.statements: list[str] = []

    def execute(self, sql, params=None):
        self.statements.append(" ".join(sql.split()))

    def fetchone(self):
        return self._row


def cursor_at(simulated: dt.date, *, profile: str = "dev", sequence: int = 0) -> FakeCursor:
    return FakeCursor((profile, 42, ANCHOR, simulated, sequence, None))


def test_a_bare_tick_is_always_permitted():
    state = require(cursor_at(ANCHOR), None, profile="dev")
    assert isinstance(state, SimulationState)
    assert state.next_date == dt.date(2026, 9, 19)


def test_the_state_row_is_locked_before_anything_is_decided():
    cursor = cursor_at(ANCHOR)
    require(cursor, None, profile="dev")
    assert cursor.statements[0].endswith("for update")


def test_exactly_the_next_day_is_permitted():
    assert require(cursor_at(ANCHOR), dt.date(2026, 9, 19), profile="dev").simulated_date == ANCHOR


def test_an_unseeded_source_is_refused_and_says_to_seed():
    with pytest.raises(TickRefusedError) as error:
        require(FakeCursor(None), None, profile="dev")
    assert "make seed" in str(error.value)


def test_a_skipped_date_is_refused_naming_the_expected_date_and_the_gap():
    with pytest.raises(TickRefusedError) as error:
        require(cursor_at(ANCHOR), dt.date(2026, 9, 25), profile="dev")
    message = str(error.value)
    assert "expected 2026-09-19" in message
    assert "skips 6 day(s)" in message
    assert "tick-to DATE=2026-09-25" in message


def test_the_current_date_is_refused_because_a_tick_is_not_idempotent():
    with pytest.raises(TickRefusedError) as error:
        require(cursor_at(ANCHOR), ANCHOR, profile="dev")
    message = str(error.value)
    assert "expected 2026-09-19" in message
    assert "not" in message and "idempotent" in message


def test_a_past_date_is_refused_naming_the_expected_date():
    with pytest.raises(TickRefusedError) as error:
        require(cursor_at(ANCHOR), dt.date(2026, 1, 1), profile="dev")
    assert "expected 2026-09-19" in str(error.value)


def test_a_profile_mismatch_is_refused_before_any_date_arithmetic():
    # Ticking a dev book as ci would read ci's parameters against dev's data, which produces a
    # coherent-looking day at the wrong scale rather than an error.
    with pytest.raises(TickRefusedError) as error:
        require(cursor_at(ANCHOR), None, profile="ci")
    message = str(error.value)
    assert "seeded at profile 'dev'" in message
    assert "NORDBANK_ENV=dev" in message


def test_the_next_date_crosses_a_month_boundary():
    state = require(cursor_at(dt.date(2026, 9, 30)), dt.date(2026, 10, 1), profile="dev")
    assert state.next_date == dt.date(2026, 10, 1)
