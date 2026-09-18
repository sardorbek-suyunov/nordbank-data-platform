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
NEEDS_QUOTING = ('"', ",", "\n", "\r")


def format_value(value: Any) -> str:
    """One CSV field for `COPY ... WITH (FORMAT CSV)`."""
    if value is None:
        return ""
    if value is True:
        return "t"
    if value is False:
        return "f"
    if isinstance(value, Decimal | int):
        return str(value)
    if isinstance(value, dt.datetime):
        # Always aware and always UTC: conventions.md prohibits naive datetimes, and the column
        # is timestamptz.
        return value.isoformat(sep=" ")
    if isinstance(value, dt.date):
        return value.isoformat()
    text = str(value)
    if any(character in text for character in NEEDS_QUOTING):
        escaped = text.replace('"', '""')
        return f'"{escaped}"'
    if text == "":
        # An empty string that is genuinely a value, not an absent one.
        return '""'
    return text


def format_row(values: Sequence[Any]) -> str:
    return ",".join(format_value(value) for value in values) + "\n"


class TableSpool:
    """The append point for one table."""

    __slots__ = ("_handle", "columns", "path", "rows", "table")

    def __init__(self, table: str, path: Path) -> None:
        self.table = table
        self.path = path
        self.columns = TABLE_COLUMNS[table]
        self.rows = 0
        self._handle = path.open("w", encoding=ENCODING, newline="")

    def write(self, values: Sequence[Any]) -> None:
        if len(values) != len(self.columns):
            raise ValueError(
                f"{self.table}: {len(values)} values for {len(self.columns)} columns"
            )
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
