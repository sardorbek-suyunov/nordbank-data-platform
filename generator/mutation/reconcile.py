"""Acceptance criterion 9: does the tick log agree with the rows the tick changed?

The criterion asks that `platform.tick_log` reconcile exactly against rows whose `updated_at`
falls within each tick's window. **Run after the sixty ticks, it cannot**, and the reason is
arithmetic rather than a defect in either side: a later tick re-stamps a row an earlier tick
wrote, and the earlier window loses it for good.

The historical book already shows the collapse, because the loader stamps an account with its
last movement. Measured on the loaded `ci` book, accounts with a movement on a day against
accounts whose `updated_at` is that day: 210 against 6 six days before the anchor, 234 against
21 three days before, 232 against 82 the day before. With 466 of 736 open accounts touched by a
single day of movements, `accounts` loses most of a six-day-old window.

**The reconciliation is exact at the tick, and that is the property M4 depends on.** An
extractor running at the end of day D sees exactly what the log says changed on day D; it does
not run in December against September's window. So this runs per tick rather than after the
run, it costs 7.8 milliseconds across five tables on the `ci` book, and the sixty-tick
acceptance run reports it for every tick.

Two rules make the identity exact rather than approximate, and both are constraints on the
change classes rather than on this module:

- **`inserted + updated` is the row count**, because `late_arriving` is a subset of `inserted`
  and `soft_deleted` a subset of `updated`. `deleted` is excluded: a physically deleted row is
  not in the table to be counted.
- **A tick never updates a row it inserted in the same tick.** Otherwise the log counts it
  twice and the window counts it once. The `dirt` phase therefore edits the population that
  was already there, and a new row that should carry dirt is written dirty rather than written
  clean and then corrected.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from ..tables import LOAD_ORDER

ONE_DAY = dt.timedelta(days=1)


@dataclass(frozen=True)
class TableReconciliation:
    table: str
    logged: int
    in_window: int

    @property
    def agrees(self) -> bool:
        return self.logged == self.in_window

    @property
    def difference(self) -> int:
        return self.in_window - self.logged


@dataclass
class TickReconciliation:
    """One tick's log against the rows in its window."""

    tick_sequence: int
    simulated_date: dt.date
    tables: list[TableReconciliation] = field(default_factory=list)

    @property
    def agrees(self) -> bool:
        return all(table.agrees for table in self.tables)

    @property
    def disagreements(self) -> list[TableReconciliation]:
        return [table for table in self.tables if not table.agrees]

    @property
    def logged(self) -> int:
        return sum(table.logged for table in self.tables)

    @property
    def in_window(self) -> int:
        return sum(table.in_window for table in self.tables)


class ReconciliationError(Exception):
    """The tick log and the rows in its window disagree. Names every table that differs."""


_WINDOW_COUNTS = " union all ".join(
    f"select '{table}' as tbl, count(*) as n from core.{table} "  # noqa: S608
    f" where updated_at >= %s and updated_at < %s"
    for table in LOAD_ORDER
)

_LOGGED = """
select c.table_name, c.rows_inserted + c.rows_updated
  from platform.tick_table_counts c
  join platform.tick_log l on l.tick_log_id = c.tick_log_id
 where l.simulated_date = %s
"""

_LATEST = (
    "select tick_sequence, simulated_date from platform.tick_log "
    "order by tick_sequence desc limit 1"
)


def reconcile(cursor: Any, simulated_date: dt.date, tick_sequence: int = 0) -> TickReconciliation:
    """Compare one tick's logged counts with the rows whose `updated_at` is in its window."""
    start = dt.datetime.combine(simulated_date, dt.time(), tzinfo=dt.UTC)
    cursor.execute(_WINDOW_COUNTS, tuple(x for _ in LOAD_ORDER for x in (start, start + ONE_DAY)))
    in_window = {table: int(count) for table, count in cursor.fetchall()}

    cursor.execute(_LOGGED, (simulated_date,))
    logged = {table: int(count) for table, count in cursor.fetchall()}

    result = TickReconciliation(tick_sequence=tick_sequence, simulated_date=simulated_date)
    for table in LOAD_ORDER:
        counted, recorded = in_window.get(table, 0), logged.get(table, 0)
        if counted or recorded:
            result.tables.append(TableReconciliation(table, recorded, counted))
    return result


def reconcile_latest(cursor: Any) -> TickReconciliation | None:
    """Reconcile the most recent tick, which is the only one nothing has had a chance to move."""
    cursor.execute(_LATEST)
    row = cursor.fetchone()
    if row is None:
        return None
    sequence, simulated_date = row
    return reconcile(cursor, simulated_date, int(sequence))


def assert_agrees(result: TickReconciliation) -> TickReconciliation:
    """Raise unless every table agrees, naming each one that does not."""
    if result.agrees:
        return result
    detail = "; ".join(
        f"core.{table.table}: log says {table.logged}, window holds {table.in_window}"
        for table in result.disagreements
    )
    raise ReconciliationError(
        f"tick {result.tick_sequence} ({result.simulated_date}) does not reconcile against its "
        f"own window: {detail}. The tick log is a reconciliation control M4 and M7 read, so a "
        f"disagreement is a defect in the control rather than a measurement."
    )


def format_reconciliation(result: TickReconciliation) -> str:
    lines = [
        f"tick {result.tick_sequence} ({result.simulated_date}): "
        f"{result.logged:,} logged against {result.in_window:,} in window"
        + ("" if result.agrees else f", {len(result.disagreements)} table(s) disagree"),
        "",
        f"  {'table':<22}{'logged':>10}{'in window':>12}{'difference':>12}",
        f"  {'-' * 22}{'-' * 9:>10}{'-' * 11:>12}{'-' * 11:>12}",
    ]
    for table in result.tables:
        lines.append(
            f"  {table.table:<22}{table.logged:>10,}{table.in_window:>12,}{table.difference:>12,}"
        )
    return "\n".join(lines)
