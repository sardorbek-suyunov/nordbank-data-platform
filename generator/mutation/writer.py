"""How a tick's rows reach the database.

Inserts go through `COPY`, like the historical load, because a day is still thousands of rows
and `INSERT` one at a time would spend the tick's whole budget on round trips. Updates go
through `UPDATE ... FROM (VALUES ...)`, one statement per jitter bucket, so a phase costs a
handful of statements rather than one per row.

**Rows are handed to `COPY` as values, not as CSV.** The loader at M2 builds CSV text because
it streams through `psql`, where there is nothing else to hand it. Here psycopg adapts each
value itself, which removes the quoting and null-rendering rules that a hand-built CSV line
has to get right — an embedded comma in a merchant name, a `None` that must be an empty field
rather than the four characters `None`. Those are exactly the bugs that survive review and
show up as a constraint violation nine tables later.

**Foreign keys stay on.** The bulk loader drops them and revalidates afterwards, measured at
63 per cent of its load time (ADR 0011). A tick writes four figures of rows, not eight, and
the measured cost with every constraint in force is well inside the budget, so dropping them
would trade a real guarantee for nothing. The deferred ledger trigger also stays: a day's
4,500 entry rows queue about 57 KB against the 512 MiB container, three orders of magnitude
below the bound ADR 0009 measured.

**Load order is the foreign key order.** With the constraints on it is not a convenience, it
is what makes the insert succeed at all: a transaction cannot reference an account the same
tick has not yet written.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from ..tables import LOAD_ORDER, TABLE_COLUMNS


class TickWriter:
    """Collects a tick's new rows and copies them in dependency order.

    Buffered to the end of the phase rather than written as they are decided, because `COPY`
    amortises over rows and because the decision order is not the foreign key order: a
    transaction's ledger batch is decided with it, but `gl_transactions` loads after
    `transactions`.
    """

    __slots__ = ("_rows",)

    def __init__(self) -> None:
        self._rows: dict[str, list[tuple]] = defaultdict(list)

    def add(self, table: str, row: tuple) -> None:
        expected = len(TABLE_COLUMNS[table])
        if len(row) != expected:
            raise ValueError(
                f"core.{table} takes {expected} values and got {len(row)}; the row and "
                f"generator/tables.py disagree about the column list"
            )
        self._rows[table].append(row)

    def count(self, table: str) -> int:
        return len(self._rows[table])

    @property
    def total(self) -> int:
        return sum(len(rows) for rows in self._rows.values())

    def flush(self, cursor: Any) -> dict[str, int]:
        """Copy every buffered table, parents before children. Returns what was written."""
        written: dict[str, int] = {}
        for table in LOAD_ORDER:
            rows = self._rows.get(table)
            if not rows:
                continue
            columns = ", ".join(TABLE_COLUMNS[table])
            with cursor.copy(f"copy core.{table} ({columns}) from stdin") as copy:
                for row in rows:
                    copy.write_row(row)
            written[table] = len(rows)
            rows.clear()
        return written


def apply_updates(cursor: Any, statement: str, *columns: list) -> int:
    """Run one set-based `UPDATE` over a group of rows and return how many changed.

    The caller passes parallel arrays, one per column, and writes the statement around
    `unnest(%s::bigint[], %s::numeric[], ...)`. Arrays rather than a `VALUES` list because
    psycopg adapts a Python list to a typed Postgres array directly, where a list of tuples
    has no unambiguous adaptation and would have to be rendered into SQL by hand — which is
    both an injection surface and the place quoting bugs live.

    One statement per group is what keeps a phase's cost proportional to the number of jitter
    buckets rather than to the number of rows.
    """
    if not columns or not columns[0]:
        return 0
    widths = {len(column) for column in columns}
    if len(widths) != 1:
        raise ValueError(f"parallel arrays differ in length: {[len(c) for c in columns]}")
    cursor.execute(statement, tuple(columns))
    return cursor.rowcount
