"""The tick command line's date arithmetic and its refusals.

No database. What is worth pinning is that `--to` produces one date per day rather than a range,
because each day owes the tick log its own window and a tick covering three days would have
three windows and one row.
"""

from __future__ import annotations

import datetime as dt

from generator.mutation.__main__ import _dates, main
from generator.mutation.state import SimulationState

ANCHOR = dt.date(2026, 9, 18)


def state(simulated: dt.date = ANCHOR) -> SimulationState:
    return SimulationState(
        profile="ci",
        seed=42,
        anchor_date=ANCHOR,
        simulated_date=simulated,
        tick_sequence=0,
        last_tick_completed_at=None,
    )


def test_a_bare_tick_asks_for_the_next_day_by_saying_nothing():
    # None means "whatever the state machine expects", which is what makes a bare `make tick`
    # always permitted and a named date always checked.
    assert _dates(state(), None, None) == [None]


def test_a_named_date_is_passed_through_for_the_state_machine_to_judge():
    requested = dt.date(2026, 9, 25)
    assert _dates(state(), requested, None) == [requested]


def test_advancing_to_a_date_produces_one_entry_per_day():
    dates = _dates(state(), None, ANCHOR + dt.timedelta(days=4))
    assert dates == [ANCHOR + dt.timedelta(days=offset) for offset in range(1, 5)]


def test_advancing_to_the_current_date_produces_nothing():
    assert _dates(state(), None, ANCHOR) == []


def test_advancing_to_a_past_date_produces_nothing_rather_than_going_backwards():
    assert _dates(state(ANCHOR + dt.timedelta(days=10)), None, ANCHOR) == []


def test_advancing_to_the_very_next_day_is_a_single_tick():
    assert _dates(state(), None, ANCHOR + dt.timedelta(days=1)) == [ANCHOR + dt.timedelta(days=1)]


def test_a_date_and_a_range_together_are_refused_before_anything_connects():
    assert main(["--date", "2026-09-19", "--to", "2026-09-25"]) == 1


def test_an_unparseable_date_is_refused_before_anything_connects():
    assert main(["--date", "the nineteenth"]) == 1


def test_an_unknown_profile_is_refused_before_anything_connects():
    assert main(["--profile", "enormous"]) == 1
