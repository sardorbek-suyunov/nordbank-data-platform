"""Validate a source record against the contract in force (spec 005 section 7).

The gate rejects records that cannot be trusted **as records**: a missing or duplicated primary
key, a value that cannot be parsed as its declared type, a structurally malformed record. It
does not enforce expectations about field content. That distinction is the whole of
`docs/architecture.md`'s bronze contract on this point and it is not a style preference: a
record rejected for being malformed is a record nothing could have used, while a record
rejected for a soft expectation is a usable record removed from the population, and because
such a condition usually persists, the same record is removed every subsequent day it changes.
The entity stops reaching bronze and every count built on it is quietly low.

A rejected record is never coerced and never dropped. It goes to quarantine with the failing
column and the reason, and `landed + quarantined = read` holds for every batch by construction
rather than by assertion — though it is asserted anyway.

Validation short-circuits on the first failing column in contract order. A quarantine row says
why the record was refused, not everything that is wrong with it; the record is out either way,
and a caller that wants the full picture has the record.
"""

from __future__ import annotations

import datetime as dt
import decimal
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

# Postgres type name to the Python types psycopg hands back for it. The source declares
# twenty-six distinct types and they collapse to six families.
#
# `bool` is checked before `int` wherever an integer is expected, because bool is a subclass of
# int in Python and `True` would otherwise pass as a bigint.
_INTEGER = ("smallint", "integer", "bigint")
_TEXTUAL = ("character varying", "character", "text")
_DECIMAL = ("numeric", "decimal")


class QuarantineReason:
    NULL_IN_NON_NULLABLE = "null in a non-nullable column"
    TYPE_MISMATCH = "value does not parse as its declared type"
    PRIMARY_KEY_NULL = "primary key is null"
    PRIMARY_KEY_DUPLICATE = "primary key is duplicated within the batch"
    MISSING_COLUMN = "column declared by the contract is absent from the record"


@dataclass(frozen=True)
class Rejection:
    """Why one record was refused."""

    column: str
    reason: str
    value: Any
    record_key: Any = None


@dataclass
class ValidationResult:
    landed: list[dict] = field(default_factory=list)
    rejected: list[tuple[dict, Rejection]] = field(default_factory=list)

    @property
    def rows_landed(self) -> int:
        return len(self.landed)

    @property
    def rows_quarantined(self) -> int:
        return len(self.rejected)

    @property
    def rows_read(self) -> int:
        return self.rows_landed + self.rows_quarantined


def _base_type(declared: str) -> str:
    """`character varying(34)` becomes `character varying`; the length is not a gate."""
    return declared.split("(", 1)[0].strip().lower()


def matches_type(value: Any, declared: str) -> bool:
    """Whether a value is of its declared type.

    Length is deliberately not checked. The declared width describes the source column, and a
    value read out of that column cannot exceed it; checking it here would instead reject the
    *token* an identifier column carries, which is 32 characters and does not have to fit the
    width of the cleartext it replaced. Tokenisation happens after validation for the same
    reason: the contract describes the value the source sent.
    """
    base = _base_type(declared)

    if base in _TEXTUAL:
        return isinstance(value, str)
    if base in _INTEGER:
        return isinstance(value, int) and not isinstance(value, bool)
    if base in _DECIMAL:
        return isinstance(value, decimal.Decimal | int) and not isinstance(value, bool)
    if base == "boolean":
        return isinstance(value, bool)
    if base == "date":
        return isinstance(value, dt.date) and not isinstance(value, dt.datetime)
    if base.startswith("timestamp"):
        return isinstance(value, dt.datetime)
    if base in ("double precision", "real"):
        return isinstance(value, float | int) and not isinstance(value, bool)
    if base == "json":
        # A nested value an authored contract carries whole, such as a FollowTheMoney entity's
        # list of names. It is a list or a mapping as the source delivered it, never a string
        # that merely looks like one.
        return isinstance(value, list | dict)
    # An unknown declared type is not silently accepted: a contract naming a type this does not
    # understand is a contract the gate cannot enforce, and saying so is better than passing
    # everything through.
    raise ValueError(f"contract declares a type this gate does not understand: {declared!r}")


def record_key(record: dict, contract):
    """The record's primary key: the value for a single-column key, a tuple for a composite."""
    keys = getattr(contract, "keys", (contract.primary_key,))
    if len(keys) == 1:
        return record.get(keys[0])
    return tuple(record.get(key) for key in keys)


def validate_record(record: dict, contract, seen_keys: set) -> Rejection | None:
    """The first reason this record is refused, or None."""
    keys = getattr(contract, "keys", (contract.primary_key,))
    key_value = record_key(record, contract)
    for key in keys:
        if record.get(key) is None:
            return Rejection(key, QuarantineReason.PRIMARY_KEY_NULL, None)
    if key_value in seen_keys:
        return Rejection(
            contract.primary_key,
            QuarantineReason.PRIMARY_KEY_DUPLICATE,
            key_value,
            record_key=key_value,
        )

    for column in contract.columns:
        if column.name not in record:
            return Rejection(
                column.name, QuarantineReason.MISSING_COLUMN, None, record_key=key_value
            )
        value = record[column.name]
        if value is None:
            if column.is_nullable:
                continue
            return Rejection(
                column.name, QuarantineReason.NULL_IN_NON_NULLABLE, None, record_key=key_value
            )
        if not matches_type(value, column.data_type):
            return Rejection(
                column.name, QuarantineReason.TYPE_MISMATCH, value, record_key=key_value
            )
    return None


def validate(records: Iterable[dict], contract) -> ValidationResult:
    """Partition a batch's records into those that land and those that are quarantined."""
    result = ValidationResult()
    seen_keys: set = set()
    for record in records:
        rejection = validate_record(record, contract, seen_keys)
        if rejection is None:
            seen_keys.add(record_key(record, contract))
            result.landed.append(record)
        else:
            result.rejected.append((record, rejection))
    return result


def project(record: dict, columns: Sequence[str]) -> dict:
    """The record reduced to the columns the contract describes, in contract order.

    This is where additive drift takes effect: a column the source has and the contract does
    not is simply not here, so bronze carries what the contract describes and an unknown column
    is noticed in `meta.schema_drift_log` rather than silently absorbed.
    """
    return {name: record.get(name) for name in columns}
