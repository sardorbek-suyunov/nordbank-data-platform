"""The Parquet schema a bronze or quarantine object is written in (spec 007 section 1).

**The contract decides the physical type, not the values.** The writer used to hand PyArrow a
dictionary of Python values and let it infer each column's type, so the type of a column was a
property of the batch rather than of the contract: a decimal column took the precision of the
widest value that happened to be in the batch, and a column null in every row of a batch was
written as Arrow `null`. Measured on the fourth acceptance run, 13 of the 49 entities with
objects carried between two and seven physical schemas, and only settlements differed because
of a contract version. A reader that combined the files positionally or by the first file's
schema failed on four of them and was right on the other nine by the luck of which file sorted
first.

Now every column's type comes from the contract version the batch was validated against:

| Declared | Written as |
|---|---|
| a character type or `text` | `string` |
| `smallint`, `integer`, `bigint` | `int16`, `int32`, `int64` |
| `numeric(p,s)` | `decimal128(p,s)` |
| `timestamp with time zone` | `timestamp[us, tz=UTC]` |
| `date`, `boolean` | `date32`, `bool` |
| `json` | the Arrow JSON type: the value serialised as JSON text |
| `double precision`, `real` | `float64` |

A `json` value is serialised because a column's physical type has to be the same for every row
whatever the value's shape, and JSON text is what the contract says the column holds. The Arrow
JSON type writes the Parquet JSON logical type, which DuckDB reads as `JSON`. It is serialised
with the key order and separators `json.dumps` gives by default, so a retry writes the same
bytes.

An identifier column holds a token, which is text whatever the cleartext's type was; every
identifier column in the contracts is a character type, so the table above already gives it.

So one contract version has one physical schema per entity, and `make ingest-integrity` checks
that no entity carries more schemas than versions.
"""

from __future__ import annotations

import json
from typing import Any


def arrow_type(declared: str):
    """The Arrow type a column declared as `declared` is written as."""
    import pyarrow as pa
    from data_contract import type_family

    family = type_family(declared)
    if family.kind == "text":
        return pa.string()
    if family.kind == "integer":
        return {16: pa.int16(), 32: pa.int32(), 64: pa.int64()}[family.precision]
    if family.kind == "decimal":
        return pa.decimal128(family.precision, family.scale)
    if family.kind == "timestamptz":
        return pa.timestamp("us", tz="UTC")
    if family.kind == "date":
        return pa.date32()
    if family.kind == "boolean":
        return pa.bool_()
    if family.kind == "json":
        return pa.json_()
    if family.kind == "double":
        return pa.float64()
    raise ValueError(f"no Arrow type for {declared!r}")


def _schema(names: tuple[str, ...], declared: dict[str, str]):
    import pyarrow as pa

    return pa.schema([pa.field(name, arrow_type(declared[name])) for name in names])


def bronze_schema(contract):
    """A bronze record of one contract version: its columns, the payload, the audit columns."""
    from data_contract import PLATFORM_COLUMN_TYPES

    declared = {**PLATFORM_COLUMN_TYPES, **{c.name: c.data_type for c in contract.columns}}
    return _schema(contract.bronze_columns, declared)


def quarantine_schema(contract):
    """A quarantine record, the one shape in every mode, with the payload for an authored one."""
    from data_contract import PLATFORM_COLUMN_TYPES

    return _schema(contract.quarantine_columns, PLATFORM_COLUMN_TYPES)


def table(rows: list[dict], schema):
    """The rows as an Arrow table of exactly `schema`; a value that does not fit it raises."""
    import pyarrow as pa

    data = {}
    for field in schema:
        values = [row.get(field.name) for row in rows]
        if isinstance(field.type, pa.JsonType):
            values = [_json_text(value) for value in values]
        data[field.name] = pa.array(values, type=field.type)
    return pa.table(data, schema=schema)


def _json_text(value: Any) -> str | None:
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)
