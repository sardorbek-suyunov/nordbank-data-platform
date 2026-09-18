"""The contract between the committed data dictionary and the live source schema.

`docs/data_dictionary.md` is the single source of truth for what the source database holds and
how each column is classified (spec 002 section 5). Three things read this module:

- `schema_apply.py`, to load `platform.column_classifications` from the dictionary;
- `schema_check.py`, to fail when the live schema and the dictionary disagree;
- `airflow/tests/test_schema_matches_dictionary.py`, which is the same comparison run inside the
  stack.

They reach the database differently, so every query goes through an executor: a callable that
takes SQL and returns rows. The host uses psql in a container, the integration test uses a
psycopg2 cursor, and neither needs the other's dependencies.

A parse failure is a hard error carrying the file, the line number and the line. It is never a
warning and never a partial load: a dictionary that half-parses would silently under-classify
columns, and an under-classified column is one the extraction layer will not tokenise.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

SCHEMAS: tuple[str, ...] = ("core", "ref", "platform")

CLASSIFICATIONS: frozenset[str] = frozenset(
    {"identifier", "quasi-identifier", "pseudonymous_key", "sensitive", "non-personal"}
)

# The dictionary's column table header, validated before any row of that table is read. A
# dictionary whose header has drifted is a dictionary whose columns may have shifted, and
# reading rows positionally out of it would load confident nonsense.
COLUMN_TABLE_HEADER: tuple[str, ...] = (
    "Column",
    "Type",
    "Nullable",
    "Classification",
    "Description",
    "Consumed by",
)

_HEADING = re.compile(r"^####\s+([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)\s*$")
_SEPARATOR = re.compile(r"^\|[\s:|-]+\|$")
_TYPE_ALLOWLIST = re.compile(r"^[a-z][a-z ]*(\(\d+(,\d+)?\))?$")
_MODIFIER = r"\(\s*\d+\s*(?:,\s*\d+\s*)?\)"

Executor = Callable[[str], list[tuple[str, ...]]]


class DictionaryError(Exception):
    """A malformed data dictionary. Carries where the problem is, not only that there is one."""

    def __init__(self, source: str, line_number: int, line: str, problem: str) -> None:
        self.source = source
        self.line_number = line_number
        self.line = line
        self.problem = problem
        super().__init__(f"{source}:{line_number}: {problem}\n  {line.rstrip()}")


@dataclass(frozen=True, order=True)
class Column:
    """One column, as the dictionary documents it or as the database holds it."""

    schema: str
    table: str
    name: str
    data_type: str
    is_nullable: bool
    classification: str = ""
    description: str = ""
    consumed_by: str = ""

    @property
    def qualified_name(self) -> str:
        return f"{self.schema}.{self.table}.{self.name}"


def _split_row(line: str) -> list[str]:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return []
    return [cell.strip() for cell in stripped[1:-1].split("|")]


def _unwrap(cell: str) -> str:
    """Drop the Markdown code ticks a readable table puts round an identifier."""
    if len(cell) > 1 and cell.startswith("`") and cell.endswith("`"):
        return cell[1:-1].strip()
    return cell


def parse_dictionary(text: str, source: str = "docs/data_dictionary.md") -> list[Column]:
    """Read every `#### <schema>.<table>` section of the dictionary into columns.

    Anything that is not such a section is ignored, so the document is free to carry entity
    inventories, value lists and prose alongside the column tables.
    """
    columns: list[Column] = []
    seen: dict[tuple[str, str, str], int] = {}
    lines = text.splitlines()
    index = 0

    while index < len(lines):
        heading = _HEADING.match(lines[index])
        if heading is None:
            index += 1
            continue

        schema, table = heading.group(1), heading.group(2)
        heading_line_number = index + 1
        if schema not in SCHEMAS:
            index += 1
            continue

        index += 1
        while index < len(lines) and not lines[index].strip():
            index += 1

        if index >= len(lines) or not lines[index].strip().startswith("|"):
            raise DictionaryError(
                source,
                heading_line_number,
                lines[heading_line_number - 1],
                f"{schema}.{table} has no column table",
            )

        header = tuple(_split_row(lines[index]))
        if header != COLUMN_TABLE_HEADER:
            raise DictionaryError(
                source,
                index + 1,
                lines[index],
                f"{schema}.{table} header is {list(header)}, expected {list(COLUMN_TABLE_HEADER)}",
            )
        index += 1

        if index >= len(lines) or not _SEPARATOR.match(lines[index].strip()):
            raise DictionaryError(
                source, index + 1, lines[index] if index < len(lines) else "", "missing separator"
            )
        index += 1

        while index < len(lines) and lines[index].strip().startswith("|"):
            cells = _split_row(lines[index])
            if len(cells) != len(COLUMN_TABLE_HEADER):
                raise DictionaryError(
                    source,
                    index + 1,
                    lines[index],
                    f"{len(cells)} cells, expected {len(COLUMN_TABLE_HEADER)}",
                )

            name, data_type, nullable, classification, description, consumed_by = cells
            name = _unwrap(name)
            data_type = _unwrap(data_type)
            classification = _unwrap(classification)

            if nullable.lower() not in {"yes", "no"}:
                raise DictionaryError(
                    source, index + 1, lines[index], f"Nullable is {nullable!r}, expected yes or no"
                )

            if classification not in CLASSIFICATIONS:
                raise DictionaryError(
                    source,
                    index + 1,
                    lines[index],
                    f"unknown classification {classification!r}; "
                    f"expected one of {sorted(CLASSIFICATIONS)}",
                )

            if not _TYPE_ALLOWLIST.match(data_type):
                raise DictionaryError(
                    source, index + 1, lines[index], f"type {data_type!r} is not a plain type name"
                )

            if not description:
                raise DictionaryError(source, index + 1, lines[index], f"{name} has no description")

            key = (schema, table, name)
            if key in seen:
                raise DictionaryError(
                    source,
                    index + 1,
                    lines[index],
                    f"{schema}.{table}.{name} is already documented at line {seen[key]}",
                )
            seen[key] = index + 1

            columns.append(
                Column(
                    schema=schema,
                    table=table,
                    name=name,
                    data_type=data_type,
                    is_nullable=nullable.lower() == "yes",
                    classification=classification,
                    description=description,
                    consumed_by=consumed_by,
                )
            )
            index += 1

    if not columns:
        raise DictionaryError(source, 1, "", "no column tables found")

    return columns


def read_dictionary(path: Path) -> list[Column]:
    return parse_dictionary(path.read_text(encoding="utf-8"), source=path.as_posix())


def live_columns(execute: Executor) -> list[Column]:
    """Every column the database actually holds, with its canonical type."""
    rows = execute(
        """
        select n.nspname,
               c.relname,
               a.attname,
               format_type(a.atttypid, a.atttypmod),
               case when a.attnotnull then 'no' else 'yes' end
          from pg_attribute a
          join pg_class c     on c.oid = a.attrelid
          join pg_namespace n on n.oid = c.relnamespace
         where n.nspname in ('core', 'ref', 'platform')
           and c.relkind = 'r'
           and a.attnum > 0
           and not a.attisdropped
         order by 1, 2, a.attnum
        """
    )
    return [
        Column(
            schema=row[0],
            table=row[1],
            name=row[2],
            data_type=row[3],
            is_nullable=row[4] == "yes",
        )
        for row in rows
    ]


def canonical_types(execute: Executor, documented: Iterable[str]) -> dict[str, str]:
    """Resolve documented type strings to the spelling `format_type` uses.

    Postgres resolves `decimal` to `numeric` and `varchar` to `character varying` at parse time,
    so the live side is already canonical and the risk is entirely on the documented side. Rather
    than maintain an alias map, whose first gap is a false failure, the database is asked:
    `to_regtype` resolves the alias and `format_type` prints the canonical name. The modifier is
    carried across from the documented string, because `to_regtype` discards it.

    Called once per distinct string, not once per column.
    """
    distinct = sorted(set(documented))
    if not distinct:
        return {}

    for data_type in distinct:
        if not _TYPE_ALLOWLIST.match(data_type):
            raise ValueError(f"type {data_type!r} is not a plain type name")

    values = ", ".join(f"('{data_type}')" for data_type in distinct)
    rows = execute(
        f"""
        with documented (t) as (values {values})
        select t,
               coalesce(
                   format_type(to_regtype(t)::oid, null)
                   || coalesce(substring(t from '{_MODIFIER}'), ''),
                   ''
               )
          from documented
        """
    )
    resolved = {row[0]: row[1] for row in rows}

    unresolved = sorted(name for name, canonical in resolved.items() if not canonical)
    if unresolved:
        raise ValueError(f"Postgres does not recognise these documented types: {unresolved}")

    return resolved


@dataclass(frozen=True)
class Difference:
    kind: str
    subject: str
    detail: str

    def __str__(self) -> str:
        return f"{self.kind}: {self.subject}{f' ({self.detail})' if self.detail else ''}"


def compare(
    documented: Sequence[Column], live: Sequence[Column], execute: Executor
) -> list[Difference]:
    """Every disagreement between the dictionary and the database, in both directions."""
    canonical = canonical_types(execute, (column.data_type for column in documented))

    documented_by_key = {(c.schema, c.table, c.name): c for c in documented}
    live_by_key = {(c.schema, c.table, c.name): c for c in live}

    differences: list[Difference] = []

    for key in sorted(live_by_key.keys() - documented_by_key.keys()):
        differences.append(
            Difference("undocumented column", ".".join(key), live_by_key[key].data_type)
        )

    for key in sorted(documented_by_key.keys() - live_by_key.keys()):
        differences.append(Difference("documented column does not exist", ".".join(key), ""))

    for key in sorted(documented_by_key.keys() & live_by_key.keys()):
        doc, actual = documented_by_key[key], live_by_key[key]
        expected_type = canonical[doc.data_type]
        if expected_type != actual.data_type:
            differences.append(
                Difference(
                    "type disagrees",
                    ".".join(key),
                    f"dictionary says {doc.data_type} ({expected_type}), database has "
                    f"{actual.data_type}",
                )
            )
        if doc.is_nullable != actual.is_nullable:
            differences.append(
                Difference(
                    "nullability disagrees",
                    ".".join(key),
                    f"dictionary says {'yes' if doc.is_nullable else 'no'}, database says "
                    f"{'yes' if actual.is_nullable else 'no'}",
                )
            )

    return differences
