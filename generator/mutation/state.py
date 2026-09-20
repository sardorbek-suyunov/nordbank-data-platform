"""`platform.simulation_state`: where the source has been advanced to, and what may advance it.

The state machine is deliberately unforgiving. `make tick DATE=D` requires the current state
to be exactly D minus one day, and refuses anything else by naming the date it expected.

**Why there is no implicit catch-up.** A tick that quietly advanced several days would make
the tick log a lie: acceptance criterion 9 reconciles each tick's counts against the rows whose
`updated_at` falls inside that tick's window, and a tick covering three days has three windows
and one row. Worse, it would hide the thing a backfill is meant to exercise — M4 needs a
sequence of single-day windows to extract, not one wide one. `make tick-to` exists to advance a
range, and it does it one transaction per day so that every day still gets its own log row.

**Why the read takes a row lock.** Two ticks starting at once would both read the same
simulated date, both decide they are advancing to the next one, and one would fail on
`tick_log_date_uq` after doing all its work. `select ... for update` on the singleton makes the
second wait for the first and then see the advanced date, so it refuses cheaply and for the
right reason.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

ONE_DAY = dt.timedelta(days=1)


class TickRefusedError(Exception):
    """A tick was asked for that the state machine will not perform. Carries why, in words."""


@dataclass(frozen=True)
class SimulationState:
    """The one row of `platform.simulation_state`."""

    profile: str
    seed: int
    anchor_date: dt.date
    simulated_date: dt.date
    tick_sequence: int
    last_tick_completed_at: dt.datetime | None

    @property
    def next_date(self) -> dt.date:
        return self.simulated_date + ONE_DAY


_COLUMNS = "profile, seed, anchor_date, simulated_date, tick_sequence, last_tick_completed_at"


def read(cursor: Any, *, for_update: bool = False) -> SimulationState | None:
    """The current state, or None when the source has never been seeded."""
    cursor.execute(
        f"select {_COLUMNS} from platform.simulation_state"  # noqa: S608 - fixed column list
        + (" for update" if for_update else "")
    )
    row = cursor.fetchone()
    return SimulationState(*row) if row else None


def require(cursor: Any, requested: dt.date | None, *, profile: str) -> SimulationState:
    """Lock the state and check it permits a tick to `requested`, or refuse saying why.

    `requested` of None means "the next day", which is what a bare `make tick` asks for and is
    always permitted. A date is checked against the one the state machine expects.
    """
    state = read(cursor, for_update=True)
    if state is None:
        raise TickRefusedError(
            "the source has no simulation state: it has not been seeded. Run `make seed` "
            "first, which loads the history and sets the simulated date to the anchor."
        )

    if state.profile != profile:
        raise TickRefusedError(
            f"the source was seeded at profile {state.profile!r} and this tick is running as "
            f"{profile!r}. Set NORDBANK_ENV={state.profile} or reseed at {profile!r}."
        )

    if requested is None or requested == state.next_date:
        return state

    if requested <= state.simulated_date:
        raise TickRefusedError(
            f"tick expected {state.next_date}; {requested} is on or before {state.simulated_date}, "
            f"which the source has already advanced through. A tick is a state transition, not "
            f"an idempotent operation, so a date cannot be replayed in place. Reseed to go back."
        )

    skipped = (requested - state.next_date).days
    raise TickRefusedError(
        f"tick expected {state.next_date}; got {requested}, which skips {skipped} day(s). "
        f"There is no implicit catch-up, because each day owes the tick log its own window. "
        f"Use `make tick-to DATE={requested}` to advance one day at a time."
    )


def reset(
    cursor: Any, *, profile: str, seed: int, anchor: dt.date, at: dt.datetime | None = None
) -> None:
    """Point the state at the anchor, creating the single row if the load is the first.

    Called by `make seed`, which has just replaced the book the state described. The tick
    sequence returns to zero and the last completion time is cleared, because they counted
    ticks against a history that no longer exists.
    """
    cursor.execute(
        """
        insert into platform.simulation_state
            (profile, seed, anchor_date, simulated_date, tick_sequence, last_tick_completed_at)
        values (%s, %s, %s, %s, 0, %s)
        -- Inferred from the expression index that makes this table a singleton. `on conflict
        -- on constraint` cannot be used: a bare unique index is not a constraint, and naming
        -- it that way fails with "constraint does not exist".
        on conflict ((true)) do update
           set profile                = excluded.profile,
               seed                   = excluded.seed,
               anchor_date            = excluded.anchor_date,
               simulated_date         = excluded.simulated_date,
               tick_sequence          = 0,
               last_tick_completed_at = excluded.last_tick_completed_at
        """,
        (profile, seed, anchor, anchor, at),
    )


def advance(cursor: Any, *, to: dt.date, completed_at: dt.datetime) -> int:
    """Move the state on one day and return the new tick sequence number.

    `completed_at` is real time, not simulated: the caller clears the simulation clock before
    this runs, so the trigger stamps `updated_at` with the wall clock like every other
    `platform` row.
    """
    cursor.execute(
        """
        update platform.simulation_state
           set simulated_date         = %s,
               tick_sequence          = tick_sequence + 1,
               last_tick_completed_at = %s
        returning tick_sequence
        """,
        (to, completed_at),
    )
    return int(cursor.fetchone()[0])
