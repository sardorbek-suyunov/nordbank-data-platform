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
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..rng import SubStreams
from . import guard as guard_module
from . import report as report_module
from . import snapshot as snapshot_module
from . import state as state_module
from .writer import TickWriter

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from source_db_driver import clear_simulation_clock, set_simulation_clock  # noqa: E402


@dataclass(frozen=True)
class PhaseClock:
    """When in the simulated day a phase's in-place updates are stamped.

    `batched` means every update in the phase shares one instant, the way a nightly job writes
    a whole sweep at the moment it ran.
    """

    start_hour: int
    end_hour: int
    batched: bool = False


# How many distinct instants a jittered phase spreads its updates over.
#
# Per-entity uniqueness is neither achievable nor wanted. The trigger reads one clock value per
# statement, so a genuinely unique instant per row would mean one statement per row — thousands
# of round trips inside the tick's transaction, to buy a property real systems do not have.
# Twenty-four buckets puts a distinct instant every few minutes of a phase's window, which
# reads as a distribution rather than as a spike, and costs one extra statement per bucket per
# table.
JITTER_BUCKETS = 24

# Phase windows, in the order the phases run.
#
# **Why the updates are spread at all.** The trigger stamps every update in a transaction with
# whatever the clock says, so a tick that set it once would give a whole day's changes one
# identical `updated_at`. That would collapse acceptance criterion 9's window to a point and
# make the tick's output look nothing like a day of a real system. Inserts are unaffected
# either way: they carry their own event instant, written explicitly, because the trigger fires
# `before update` only.
#
# **Why `lifecycle` is deliberately not spread.** `architecture.md` justifies M4's `>=` overlap
# on the extraction watermark partly by a specific failure: several rows share the exact
# `updated_at` that became the watermark, and a strict greater-than reads one of them and skips
# the rest for good. If every timestamp this engine produces were distinct, that case would
# never arise and no test at M4 could exercise the defence against it. The account and card
# status sweep therefore writes its whole batch at one identical instant, which is also what a
# nightly batch job does, so the realistic choice and the testable one are the same choice.
#
# The windows are plausible rather than arbitrary: an overnight sweep, onboarding during
# office hours, movements across the whole day, analyst dispositions in the working afternoon,
# back-office corrections after it, a purge in the evening, and a migration last.
PHASE_CLOCKS: dict[str, PhaseClock] = {
    "lifecycle": PhaseClock(2, 3, batched=True),
    "acquisition": PhaseClock(9, 17),
    "movements": PhaseClock(0, 24),
    "dispositions": PhaseClock(9, 18),
    "dirt": PhaseClock(14, 20),
    "deletes": PhaseClock(20, 22),
    "drift": PhaseClock(23, 24),
}

# A failure can be injected after any phase, which is how acceptance criterion 2 is
# demonstrated: the hook is a Python keyword argument with no command line flag and no
# environment variable behind it, so it cannot be reached from an operator's shell by accident.
PHASES: tuple[str, ...] = tuple(PHASE_CLOCKS)


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
    snapshot: snapshot_module.Snapshot
    # One ledger for the whole tick. Two phases post monetary events — acquisition's opening
    # deposits and everything the movement phase does — and two writers starting from the same
    # high-water mark would allocate the same batch id.
    ledger: Any = None
    # Rows the phases have decided but not yet copied. Flushed at the end of every phase, so a
    # later phase can update what an earlier one inserted.
    writer: TickWriter = field(default_factory=TickWriter)
    # Accounts whose balance this tick moved, declared by whatever moved them. The guard
    # re-derives each one's balance from scratch rather than trusting the delta, so this set
    # says where to look and nothing more.
    touched_accounts: set[int] = field(default_factory=set)
    # Loan cash flows the lifecycle phase has decided and the movement phase has to fold into
    # the balance. The seam exists because a repayment is two facts in two phases: the
    # installment is marked paid, which is a loan fact, and the money leaves the account, which
    # is a movement. The historical pipeline splits them the same way, and invariant 4 requires
    # the second to be a transaction rather than a kind of event of its own.
    cash_events: list[Any] = field(default_factory=list)

    def stream(self, name: str, *key: object):
        """A random stream for `name` at `key`, scoped to this tick's date."""
        return self.streams.stream(f"tick.{name}", self.simulated_date.isoformat(), *key)

    def entity_stream(self, name: str, *key: object):
        """A random stream for an entity, the same on every tick that looks at it.

        **A hazard may be redrawn each tick; a lag must not.** `stream` is keyed on the
        simulated date, so a value drawn from it is a fresh draw every day — which is what a
        daily hazard wants and is exactly wrong for the length of time something takes. A lag
        redrawn each tick is realised as the *minimum* over repeated draws: an alert whose
        disposition lag was drawn from `stream` disposed on the first day a fresh draw happened
        to have elapsed, so a stated one-to-fourteen-day lag measured at a mean of 4.4 days and
        never once exceeded nine over forty-two observations.

        Nothing in the schema records when a case is due, and adding a column would put a
        simulation detail in the bank's data, so the lag is a pure function of the seed and the
        entity's key and is recomputed identically on every tick that asks.
        """
        return self.streams.stream(f"tick.{name}", *key)

    @property
    def window(self) -> tuple[dt.datetime, dt.datetime]:
        """The half-open span this tick's `updated_at` values fall in: the simulated day."""
        return self.at(0), self.at(0) + dt.timedelta(days=1)

    def set_clock(self, moment: dt.datetime) -> None:
        """Point the `updated_at` trigger at `moment` for the statements that follow.

        A phase calls this before an UPDATE whose rows should carry an instant other than the
        one the tick set on its behalf. It holds until the next call or until the tick clears
        the clock before writing `platform`.
        """
        set_simulation_clock(self.cursor, moment)

    def at(self, hour: int = 0, minute: int = 0, second: int = 0) -> dt.datetime:
        """An instant inside the simulated day."""
        return dt.datetime.combine(
            self.simulated_date, dt.time(hour, minute, second), tzinfo=dt.UTC
        )

    def phase_instant(self, phase: str, bucket: int = 0) -> dt.datetime:
        """The instant a given jitter bucket of `phase` stamps its updates with.

        A batched phase ignores the bucket and returns its window's start, so every row it
        writes shares one `updated_at`. That tie is deliberate; see PHASE_CLOCKS.
        """
        clock = PHASE_CLOCKS[phase]
        if clock.batched:
            return self.at(clock.start_hour)
        span = (clock.end_hour - clock.start_hour) * 3600
        offset = int(span * (bucket + 0.5) / JITTER_BUCKETS)
        return self.at(clock.start_hour) + dt.timedelta(seconds=offset)

    def bucket_of(self, phase: str, key: object) -> int:
        """Which jitter bucket an entity falls in, drawn on its own substream.

        On the entity's own stream rather than round-robin, so the assignment is a property of
        the entity and the seed rather than of the order rows came back from the database.
        """
        if PHASE_CLOCKS[phase].batched:
            return 0
        return self.stream(f"jitter.{phase}", key).randrange(JITTER_BUCKETS)

    def jitter_groups(
        self, phase: str, items: Iterable[Any], key_of: Callable[[Any], object]
    ) -> list[tuple[dt.datetime, list[Any]]]:
        """Group `items` into jitter buckets and return them in time order.

        The caller sets the clock to each instant and issues one statement for its group, so a
        phase costs one statement per non-empty bucket rather than one per row. A batched
        phase yields a single group, which is the point of it.
        """
        buckets: dict[int, list[Any]] = {}
        for item in items:
            buckets.setdefault(self.bucket_of(phase, key_of(item)), []).append(item)
        return [(self.phase_instant(phase, bucket), buckets[bucket]) for bucket in sorted(buckets)]


# A change class takes the context and records what it did on the report. The table is passed
# in rather than assembled by decorators at import time: a registry populated by side effect
# makes the set of phases depend on which modules happened to be imported, which is a fine way
# to lose a change class silently. `generator.mutation.changes` holds the real table.
ChangePhase = Callable[[TickContext], None]


def _ledger_for(context: TickContext) -> Any:
    """The tick's double-entry writer, continuing the ledger's own key sequence."""
    from ..entities.ledger import LedgerWriter  # noqa: PLC0415 - the import is the wiring

    return LedgerWriter(
        context.writer,
        context.profile.params["ledger"]["accounts"],
        first_batch_id=context.snapshot.next_id["gl_transactions"],
        first_entry_id=context.snapshot.next_id["gl_entries"],
    )


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
            snapshot=snapshot_module.read(
                cursor,
                target,
                anchor_date=current.anchor_date,
                history_months=profile.history_months,
            ),
        )

        context.ledger = _ledger_for(context)

        # core and ref carry simulated time from here, re-stamped per phase so that the tick's
        # changes spread across the simulated day instead of sharing one instant.
        for name in PHASES:
            # A phase that stamps nothing itself still starts from a defined instant, so a
            # handler that issues a bare UPDATE without grouping does not inherit the previous
            # phase's clock.
            set_simulation_clock(cursor, context.phase_instant(name))
            handler = handlers.get(name)
            if handler is not None:
                handler(context)
                # Insert counts come from what COPY actually wrote rather than from what the
                # handler believed it decided. The tick log is a reconciliation control, and a
                # control whose counts are a second opinion on the write is not one.
                for table, written in context.writer.flush(cursor).items():
                    report.record(table, "inserted", written)
            if fail_after == name:
                raise InducedFailureError(
                    f"failure induced after the {name!r} phase of tick {target}. Nothing is "
                    f"committed: the simulation date and every row are unchanged."
                )

        # The delta guard, inside the transaction and before anything is committed. A guard
        # whose failure left the tick committed would be a log line about a database that is
        # already wrong, so this raises and the caller's rollback is what happens next.
        guard_result = guard_module.run(
            cursor,
            touched_accounts=context.touched_accounts,
            window=context.window,
            recurring_prices=[
                float(price) for price in profile.params["amounts"]["recurring_amount_choices"]
            ],
            sample_rng=context.stream("guard.balances"),
        )
        guard_module.assert_coverage(
            guard_result, profile.band("login_precedes_transaction_share")[0]
        )
        report.guard = guard_result

        # platform carries real time from here. Clearing holds for the rest of the
        # transaction, which is why every platform write is below this line.
        clear_simulation_clock(cursor)

        report.completed_at = dt.datetime.now(dt.UTC)
        report.tick_sequence = state_module.advance(
            cursor, to=target, completed_at=report.completed_at
        )
        report_module.write(cursor, report)
        if report.drift_fired:
            from .. import drift as drift_module  # noqa: PLC0415 - the import is the wiring

            drift_module.record_fired(
                cursor,
                report.drift_fired,
                simulated_date=target,
                tick_sequence=report.tick_sequence,
                applied_at=report.completed_at,
            )

    connection.commit()
    return report
