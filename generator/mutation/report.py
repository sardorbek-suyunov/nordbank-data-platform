"""What a tick changed, counted by table and operation class, and written to `platform`.

This is not telemetry. At M4 the platform asserts that bronze received exactly what the source
says it changed, and at M7 that becomes a reconciliation check, so the counts are designed for
that consumer: one row per table per tick, with the operation classes M4 has to distinguish.

Two of the five classes are deliberately not disjoint, and conflating them would be the easy
mistake:

- `late_arriving` is a **subset of** `inserted`. A row inserted today with a business timestamp
  three days old is one insert, counted twice — once as an insert because bronze will receive
  it as one, and once as a late arrival because silver has to order it by business time. A
  check constraint enforces the subset relation so the two cannot drift apart.
- `soft_deleted` is a **subset of** `updated`. Setting `is_deleted` is an update, and the row
  reaches bronze through the normal incremental path exactly like any other change.

`deleted` is the one disjoint class and the only one that removes anything: the single physical
delete the source performs, whose keys are recorded individually so M7's reconciler has a known
positive set rather than a number.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

# The order the counts are reported in, and the order the columns sit in on
# platform.tick_table_counts.
CLASSES = ("inserted", "updated", "soft_deleted", "late_arriving", "deleted")

_COUNT_COLUMNS = (
    "rows_inserted",
    "rows_updated",
    "rows_soft_deleted",
    "rows_late_arriving",
    "rows_deleted",
)


@dataclass
class TickReport:
    """Counts for one tick, accumulated as the change classes run."""

    simulated_date: dt.date
    profile: str
    seed: int
    tick_sequence: int = 0
    started_at: dt.datetime | None = None
    completed_at: dt.datetime | None = None
    counts: dict[str, dict[str, int]] = field(default_factory=lambda: defaultdict(dict))
    deleted_keys: list[tuple[str, int]] = field(default_factory=list)
    drift_fired: list[str] = field(default_factory=list)

    def record(self, table: str, klass: str, count: int = 1) -> None:
        if klass not in CLASSES:
            raise ValueError(f"unknown operation class {klass!r}; expected one of {CLASSES}")
        if count:
            self.counts[table][klass] = self.counts[table].get(klass, 0) + count

    def record_delete(self, table: str, key: int) -> None:
        """A physical delete, counted and with its key kept for M7's reconciler."""
        self.record(table, "deleted")
        self.deleted_keys.append((table, int(key)))

    def total(self, klass: str) -> int:
        return sum(counts.get(klass, 0) for counts in self.counts.values())

    @property
    def duration_ms(self) -> int:
        if self.started_at is None or self.completed_at is None:
            return 0
        return max(0, int((self.completed_at - self.started_at).total_seconds() * 1000))

    @property
    def rows_touched(self) -> int:
        """Rows the tick wrote, counting each row once.

        `late_arriving` is excluded because it is a subset of `inserted`, and `soft_deleted`
        because it is a subset of `updated`. Adding all five would double count.
        """
        return self.total("inserted") + self.total("updated") + self.total("deleted")


def write(cursor: Any, report: TickReport) -> None:
    """Persist the report. Called after the simulation clock is cleared, so it carries real time.

    Every statement here writes a `platform` row, which is why the caller clears the clock
    first: a reconciliation control that recorded the simulated date as its run time would be
    useless to M7, which needs to know when the tick actually happened.
    """
    cursor.execute(
        """
        insert into platform.tick_log
            (tick_sequence, simulated_date, profile, seed, started_at, completed_at, duration_ms)
        values (%s, %s, %s, %s, %s, %s, %s)
        returning tick_log_id
        """,
        (
            report.tick_sequence,
            report.simulated_date,
            report.profile,
            report.seed,
            report.started_at,
            report.completed_at,
            report.duration_ms,
        ),
    )
    tick_log_id = cursor.fetchone()[0]

    if report.counts:
        columns = ", ".join(_COUNT_COLUMNS)
        rows = [
            (tick_log_id, table, *(counts.get(klass, 0) for klass in CLASSES))
            for table, counts in sorted(report.counts.items())
        ]
        cursor.executemany(
            f"insert into platform.tick_table_counts (tick_log_id, table_name, {columns}) "  # noqa: S608
            f"values (%s, %s, %s, %s, %s, %s, %s)",
            rows,
        )

    if report.deleted_keys:
        cursor.executemany(
            "insert into platform.tick_deleted_keys (tick_log_id, table_name, deleted_key) "
            "values (%s, %s, %s)",
            [(tick_log_id, table, key) for table, key in sorted(report.deleted_keys)],
        )


def format_report(report: TickReport) -> str:
    """The per-tick summary `make tick` prints."""
    lines = [
        f"tick {report.tick_sequence}: {report.simulated_date} "
        f"({report.profile}, seed {report.seed}) in {report.duration_ms} ms",
        "",
        f"  {'table':<22}{'insert':>9}{'update':>9}{'soft del':>10}{'late':>8}{'delete':>8}",
        f"  {'-' * 22}{' ' + '-' * 8:>9}{' ' + '-' * 8:>9}{' ' + '-' * 9:>10}"
        f"{' ' + '-' * 7:>8}{' ' + '-' * 7:>8}",
    ]
    for table, counts in sorted(report.counts.items()):
        lines.append(
            f"  {table:<22}"
            + "".join(
                f"{counts.get(klass, 0):>{width},}"
                for klass, width in zip(CLASSES, (9, 9, 10, 8, 8), strict=True)
            )
        )
    lines.append(
        f"  {'-' * 22}{' ' + '-' * 8:>9}{' ' + '-' * 8:>9}{' ' + '-' * 9:>10}"
        f"{' ' + '-' * 7:>8}{' ' + '-' * 7:>8}"
    )
    lines.append(
        f"  {'total':<22}"
        + "".join(
            f"{report.total(klass):>{width},}"
            for klass, width in zip(CLASSES, (9, 9, 10, 8, 8), strict=True)
        )
    )
    if report.drift_fired:
        lines.append("")
        lines.append(f"  drift fired: {', '.join(report.drift_fired)}")
    return "\n".join(lines)
