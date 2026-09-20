"""One tick: advance the simulated source by one business day, in one transaction.

The order inside the transaction is not arbitrary and two parts of it are load-bearing.

**The simulation clock is set once, at the top, and cleared exactly once before the `platform`
writes at the bottom.** Clearing is not scoped to the next statement — it holds for the rest of
the transaction — so this is an ordering requirement rather than a toggle. Everything between
the two carries simulated time, which is the entire point of the milestone; everything after
carries real time, because `platform.tick_log` is a reconciliation control M7 reads and a
control that lies about when it ran is useless.

**The state row is locked before anything is decided.** Two ticks starting at once would
otherwise both read the same date, both do a day's work, and one would fail at the very end on
the unique constraint. The lock makes the second wait and then refuse cheaply, for the right
reason, having written nothing.

The change classes run between those two points, in dependency order: what a later class needs
to see must already exist. New customers arrive before the accounts that belong to them, and
movements post after the lifecycle changes that decide which accounts are open to receive them.
"""

from __future__ import annotations

import datetime as dt
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..rng import SubStreams
from . import report as report_module
from . import state as state_module

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from source_db_driver import clear_simulation_clock, set_simulation_clock  # noqa: E402

# Phase names in the order they run, each with the hour of the simulated day its in-place
# updates are stamped with.
#
# **The hours are why acceptance criterion 9 has a window to reconcile against rather than a
# point.** The trigger stamps every update in a transaction with whatever the clock says, so a
# tick that set it once would give a whole day's changes one identical `updated_at`. That is
# not wrong — architecture.md's extraction reads `updated_at >= watermark - lag` precisely
# because rows can share an instant — but it would collapse the tick's window to a single
# value and leave the overlap logic with nothing to exercise. Inserts are unaffected either
# way: they carry their own event instant, written explicitly, because the trigger fires
# `before update` only.
#
# The hours are plausible rather than arbitrary. Lifecycle changes are an overnight batch,
# acquisition happens in the working day, movements post through it, analyst dispositions land
# in the afternoon, and the back-office corrections that produce dirt come last.
PHASE_HOURS: dict[str, int] = {
    "lifecycle": 6,
    "acquisition": 9,
    "movements": 12,
    "dispositions": 16,
    "dirt": 18,
    "deletes": 20,
    "drift": 22,
}

# A failure can be injected after any phase, which is how acceptance criterion 2 is
# demonstrated: the hook is a Python keyword argument with no command line flag and no
# environment variable behind it, so it cannot be reached from an operator's shell by accident.
PHASES: tuple[str, ...] = tuple(PHASE_HOURS)


class InducedFailureError(RuntimeError):
    """Raised by the failure hook. Exists so a test can prove the transaction is atomic."""


@dataclass
class TickContext:
    """Everything a change class needs, assembled once per tick.

    `streams` is derived from the run seed and the simulated date alone, so tick(D) produces
    the same result regardless of how many ticks preceded it. That is what makes a tick
    replayable without being idempotent.
    """

    cursor: Any
    simulated_date: dt.date
    profile: Any
    seed: int
    streams: SubStreams
    report: report_module.TickReport

    def stream(self, name: str, *key: object):
        """A random stream for `name` at `key`, scoped to this tick's date."""
        return self.streams.stream(f"tick.{name}", self.simulated_date.isoformat(), *key)

    def at(self, hour: int = 0, minute: int = 0, second: int = 0) -> dt.datetime:
        """An instant inside the simulated day."""
        return dt.datetime.combine(
            self.simulated_date, dt.time(hour, minute, second), tzinfo=dt.UTC
        )


# A change class takes the context and records what it did on the report. The table is passed
# in rather than assembled by decorators at import time: a registry populated by side effect
# makes the set of phases depend on which modules happened to be imported, which is a fine way
# to lose a change class silently. `generator.mutation.changes` holds the real table.
ChangePhase = Callable[[TickContext], None]


def run(
    connection: Any,
    *,
    requested_date: dt.date | None,
    profile: Any,
    handlers: Mapping[str, ChangePhase] | None = None,
    now: dt.datetime | None = None,
    fail_after: str | None = None,
) -> report_module.TickReport:
    """Advance the source one day and return what changed. Commits on success.

    `fail_after` raises after the named phase, before anything is committed. It exists for the
    test that demonstrates acceptance criterion 2 and has no operator-facing surface.
    """
    if fail_after is not None and fail_after not in PHASES:
        raise ValueError(f"unknown phase {fail_after!r}; expected one of {PHASES}")

    if handlers is None:
        from .changes import HANDLERS  # noqa: PLC0415 - the import is the wiring, not a cycle

        handlers = HANDLERS
    unknown = sorted(set(handlers) - set(PHASES))
    if unknown:
        raise ValueError(f"unknown phase(s) {unknown}; expected a subset of {list(PHASES)}")

    started = now or dt.datetime.now(dt.UTC)

    with connection.cursor() as cursor:
        # Lock first, decide second. Nothing below this line runs for a tick that will be
        # refused.
        current = state_module.require(cursor, requested_date, profile=profile.name)
        target = requested_date or current.next_date

        report = report_module.TickReport(
            simulated_date=target,
            profile=current.profile,
            seed=current.seed,
            started_at=started,
        )
        context = TickContext(
            cursor=cursor,
            simulated_date=target,
            profile=profile,
            seed=current.seed,
            streams=SubStreams(current.seed),
            report=report,
        )

        # core and ref carry simulated time from here, re-stamped per phase so that the tick's
        # changes spread across the simulated day instead of sharing one instant.
        for name in PHASES:
            set_simulation_clock(cursor, context.at(PHASE_HOURS[name]))
            handler = handlers.get(name)
            if handler is not None:
                handler(context)
            if fail_after == name:
                raise InducedFailureError(
                    f"failure induced after the {name!r} phase of tick {target}. Nothing is "
                    f"committed: the simulation date and every row are unchanged."
                )

        # platform carries real time from here. Clearing holds for the rest of the
        # transaction, which is why every platform write is below this line.
        clear_simulation_clock(cursor)

        report.completed_at = dt.datetime.now(dt.UTC)
        report.tick_sequence = state_module.advance(
            cursor, to=target, completed_at=report.completed_at
        )
        report_module.write(cursor, report)

    connection.commit()
    return report
