"""Command line entry point for the mutation engine.

    python -m generator.mutation                  # advance one day
    python -m generator.mutation --date 2026-09-19
    python -m generator.mutation --to 2026-11-17  # one transaction per day, up to X
    python -m generator.mutation --status

`make tick`, `make tick-to` and `make tick-status` are the operator-facing names.

**`--to` is a loop over single ticks, not a wide one.** Each day is its own transaction and its
own row in `platform.tick_log`, because acceptance criterion 9 reconciles each tick's counts
against the rows whose `updated_at` falls in that tick's window — and a tick covering three days
has three windows and one row. It is also what M4's backfill needs: a sequence of single-day
windows to extract, not one wide one.

**The reconciliation runs after every tick, not at the end.** A later tick re-stamps a row an
earlier one wrote, so the identity holds at the tick and nowhere else. A failure raises, which
is the point: the tick log is a control M4 and M7 read, and one that disagreed with the database
silently would be worse than none.

`require_stack()` is deliberately not called. It costs a measured 530 ms of `docker compose ps`
against a two-second per-tick budget, and the connection failure path carries the same diagnosis
a second later (spec 004, amended).
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from source_db_driver import SourceDatabaseError, connect  # noqa: E402

from ..config import ConfigError, load_profile  # noqa: E402
from . import reconcile as reconcile_module  # noqa: E402
from . import report as report_module  # noqa: E402
from . import snapshot as snapshot_module  # noqa: E402
from . import state as state_module  # noqa: E402
from . import tick as tick_module  # noqa: E402
from .guard import GuardFailedError  # noqa: E402

STATUS_ROWS = 10


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m generator.mutation",
        description="Advance the simulated source system by one business day.",
    )
    parser.add_argument("--profile", default=os.environ.get("NORDBANK_ENV", "dev"))
    parser.add_argument("--date", default="", help="the single date to advance to")
    parser.add_argument("--to", default="", help="advance one day at a time up to this date")
    parser.add_argument(
        "--status", action="store_true", help="print the simulation state and recent ticks"
    )
    parser.add_argument(
        "--quiet", action="store_true", help="one line per tick instead of the full table"
    )
    return parser.parse_args(argv)


def _status(connection) -> int:
    with connection.cursor() as cursor:
        state = state_module.read(cursor)
        if state is None:
            print(
                "tick-status: the source has no simulation state; it has not been seeded",
                file=sys.stderr,
            )
            return 1
        print(
            f"tick-status: profile {state.profile}, seed {state.seed}, "
            f"anchor {state.anchor_date}, simulated date {state.simulated_date}, "
            f"{state.tick_sequence} tick(s) completed"
        )
        if state.last_tick_completed_at:
            print(f"tick-status: last tick completed at {state.last_tick_completed_at}")

        cursor.execute(
            """
            select l.tick_sequence, l.simulated_date, l.duration_ms,
                   coalesce(sum(c.rows_inserted), 0), coalesce(sum(c.rows_updated), 0),
                   coalesce(sum(c.rows_soft_deleted), 0), coalesce(sum(c.rows_late_arriving), 0),
                   coalesce(sum(c.rows_deleted), 0)
              from platform.tick_log l
              left join platform.tick_table_counts c on c.tick_log_id = l.tick_log_id
             group by l.tick_log_id, l.tick_sequence, l.simulated_date, l.duration_ms
             order by l.tick_sequence desc
             limit %s
            """,
            (STATUS_ROWS,),
        )
        rows = cursor.fetchall()
        if rows:
            print()
            print(
                f"  {'tick':>5}  {'date':<12}{'ms':>7}{'insert':>9}{'update':>9}"
                f"{'soft del':>10}{'late':>7}{'delete':>8}"
            )
            print(
                f"  {'-' * 5}  {'-' * 12}{'-' * 6:>7}{'-' * 8:>9}{'-' * 8:>9}"
                f"{'-' * 9:>10}{'-' * 6:>7}{'-' * 7:>8}"
            )
            for sequence, date, duration, ins, upd, soft, late, deleted in rows:
                print(
                    f"  {sequence:>5}  {str(date):<12}{duration:>7,}{ins:>9,}{upd:>9,}"
                    f"{soft:>10,}{late:>7,}{deleted:>8,}"
                )

        cursor.execute(
            "select event_name, simulated_date, tick_sequence from platform.drift_log "
            "order by drift_log_id"
        )
        fired = cursor.fetchall()
        if fired:
            print()
            for name, date, sequence in fired:
                print(f"  drift: {name} fired on {date} at tick {sequence}")
    return 0


def _dates(state, requested: dt.date | None, upto: dt.date | None) -> list[dt.date | None]:
    """Which dates this invocation advances through."""
    if upto is None:
        return [requested]
    day = state.next_date
    out: list[dt.date | None] = []
    while day <= upto:
        out.append(day)
        day += dt.timedelta(days=1)
    return out


def _run_ticks(connection, profile, dates: list[dt.date | None], *, quiet: bool) -> int:
    timings: list[float] = []
    for target in dates:
        started = time.perf_counter()
        report = tick_module.run(connection, requested_date=target, profile=profile)
        timings.append(time.perf_counter() - started)

        # The reconciliation is exact at the tick and nowhere else, so it runs here. Its own
        # read is rolled back: nothing it does belongs in the next tick's transaction.
        with connection.cursor() as cursor:
            reconciliation = reconcile_module.reconcile_latest(cursor)
        connection.rollback()
        if reconciliation is not None:
            reconcile_module.assert_agrees(reconciliation)

        if quiet:
            print(
                f"tick {report.tick_sequence:>3} {report.simulated_date}: "
                f"{report.rows_touched:>7,} rows in {timings[-1] * 1000:6.0f} ms"
            )
        else:
            print(report_module.format_report(report))
            print()

    with connection.cursor() as cursor:
        # After the commit, never inside it: `setval` is not transactional, so a tick that
        # resynchronised inside its own transaction would leave a sequence advanced after a
        # rollback (spec 004, amended).
        snapshot_module.synchronise_sequences(cursor)
    connection.commit()

    if len(timings) > 1:
        total = sum(timings)
        print(
            f"tick: {len(timings)} tick(s) in {total:.1f} s: min {min(timings) * 1000:.0f} ms, "
            f"mean {total / len(timings) * 1000:.0f} ms, max {max(timings) * 1000:.0f} ms"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    try:
        profile = load_profile(args.profile)
    except ConfigError as error:
        print(f"tick: {error}", file=sys.stderr)
        return 1

    try:
        requested = dt.date.fromisoformat(args.date) if args.date else None
        upto = dt.date.fromisoformat(args.to) if args.to else None
    except ValueError as error:
        print(f"tick: {error}", file=sys.stderr)
        return 1

    if requested and upto:
        print("tick: --date and --to are alternatives; pass one", file=sys.stderr)
        return 1

    try:
        with connect() as connection:
            if args.status:
                return _status(connection)

            with connection.cursor() as cursor:
                state = state_module.read(cursor)
            connection.rollback()
            if state is None:
                raise state_module.TickRefusedError(
                    "the source has no simulation state: it has not been seeded. Run "
                    "`make seed` first, which loads the history and sets the simulated date "
                    "to the anchor."
                )

            dates = _dates(state, requested, upto)
            if not dates:
                print(
                    f"tick-to: the source is already at {state.simulated_date}; "
                    f"{upto} is not ahead of it"
                )
                return 0
            return _run_ticks(connection, profile, dates, quiet=args.quiet or len(dates) > 3)
    except state_module.TickRefusedError as error:
        print(f"tick: refused. {error}", file=sys.stderr)
        return 1
    except GuardFailedError as error:
        print(f"tick: {error}", file=sys.stderr)
        return 1
    except reconcile_module.ReconciliationError as error:
        print(f"tick: {error}", file=sys.stderr)
        return 1
    except SourceDatabaseError as error:
        print(f"tick: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
