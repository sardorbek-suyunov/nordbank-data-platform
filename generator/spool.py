"""Row sink: one CSV file per table, written as rows are generated.

The generator streams. Holding a full profile's rows in memory would work at `ci` and fail at
`full`, and a design that only works at one scale is what spec 003 section 1 calls a defect.
Rows go to disk as they are produced and the loader copies each file in dependency order.

Formatting is fixed here rather than by a CSV module so that NULL and the empty string stay
distinguishable: `COPY ... WITH (FORMAT CSV)` reads an unquoted empty field as NULL and a
quoted one as an empty string, and a writer that quoted everything would turn every absent
value into an empty string that fails a not-null constraint or, worse, does not.
"""

from __future__ import annotations

import datetime as dt
import shutil
from collections.abc import Iterable, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from .tables import TABLE_COLUMNS

# Written and read by psql, which is UTF-8 in this stack.
ENCODING = "utf-8"

# One mebibyte rather than the default eight kibibytes. The full profile writes gigabytes
# a row at a time, and on this platform the syscall per buffer flush dominated generation:
# the dev profile ran at about 8,000 rows a second against 62,000 for the ci profile, whose
# whole output fits in the page cache. The buffer is the difference.
WRITE_BUFFER_BYTES = 1 << 20
NEEDS_QUOTING = ('"', ",", "\n", "\r")


def format_value(value: Any) -> str:
    """One CSV field for `COPY ... WITH (FORMAT CSV)`.

    Written for speed, because it is the hot path: a profile of the ci run put this function and
    the two generator expressions that fed it at about half of total generation time, ahead of
    every random draw in the program. The shape it is written in follows from that.

    Dispatch is on `value.__class__` rather than `isinstance`, which is an exact match and skips
    the subclass walk; the order is by how often each type actually appears in a row. `bool` is
    checked before `int` even though it is a subclass of one, because an exact class match on
    `int` never sees a bool and the audit columns make booleans common. The quoting test is four
    literal `in` tests rather than `any()` over a tuple, which keeps it in C. The `isinstance`
    tail catches anything the exact checks missed, so behaviour is unchanged for subclasses.
    """
    if value is None:
        return ""

    cls = value.__class__
    if cls is str:
        if '"' in value or "," in value or "\n" in value or "\r" in value:
            return '"' + value.replace('"', '""') + '"'
        # An empty string that is genuinely a value, not an absent one. COPY reads an unquoted
        # empty field as NULL, so this is the difference between the two.
        return value if value else '""'
    if cls is Decimal or cls is int:
        return str(value)
    if cls is bool:
        return "t" if value else "f"
    if cls is dt.datetime:
        # Always aware and always UTC: conventions.md prohibits naive datetimes, and the column
        # is timestamptz.
        return value.isoformat(sep=" ")
    if cls is dt.date:
        return value.isoformat()

    if isinstance(value, bool):
        return "t" if value else "f"
    if isinstance(value, Decimal | int):
        return str(value)
    if isinstance(value, dt.datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, dt.date):
        return value.isoformat()
    return format_value(str(value))


def format_row(values: Sequence[Any]) -> str:
    # A list comprehension rather than a generator expression: join has to materialise the
    # sequence either way, and building the list directly is measurably faster.
    return ",".join([format_value(value) for value in values]) + "\n"


class TableSpool:
    """The append point for one table."""

    __slots__ = ("_handle", "columns", "path", "rows", "table")

    def __init__(self, table: str, path: Path) -> None:
        self.table = table
        self.path = path
        self.columns = TABLE_COLUMNS[table]
        self.rows = 0
        self._handle = path.open("w", encoding=ENCODING, newline="", buffering=WRITE_BUFFER_BYTES)

    def write(self, values: Sequence[Any]) -> None:
        if len(values) != len(self.columns):
            raise ValueError(f"{self.table}: {len(values)} values for {len(self.columns)} columns")
        self._handle.write(format_row(values))
        self.rows += 1

    def write_many(self, rows: Iterable[Sequence[Any]]) -> None:
        for row in rows:
            self.write(row)

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.close()


class Spool:
    """A directory of per-table CSV files for one run."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._tables: dict[str, TableSpool] = {}

    def table(self, name: str) -> TableSpool:
        if name not in self._tables:
            if name not in TABLE_COLUMNS:
                raise KeyError(f"{name} is not a core table")
            self._tables[name] = TableSpool(name, self.root / f"{name}.csv")
        return self._tables[name]

    def counts(self) -> dict[str, int]:
        return {name: spool.rows for name, spool in sorted(self._tables.items())}

    def path(self, name: str) -> Path:
        return self.root / f"{name}.csv"

    def close(self) -> None:
        for spool in self._tables.values():
            spool.close()

    def remove(self) -> None:
        self.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def __enter__(self) -> Spool:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
