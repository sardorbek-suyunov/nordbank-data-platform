"""Cast a delivered string to its declared type, or say why it cannot be (spec 006 section 2).

A relational source hands the extractor typed values, which is why specification 005 could only
inject a cast failure. A file hands it strings, and a string that does not parse as its declared
type is exactly the malformed record a third party sends. The cast is strict: nothing is
coerced, rounded, trimmed or guessed, because a value the platform repaired would land looking
like one the sender sent.

An empty field is a null, and whether a null is acceptable is the contract's nullability rule,
applied afterwards by the validator — a blank required field is refused there, with that reason,
rather than here as a parse failure. A contract may declare other null markers, as the FRED
contract does for ".", so that a marker the publisher documents becomes a null by the contract's
rule rather than by the gate's leniency.
"""

from __future__ import annotations

import datetime as dt
import decimal
import re

_INTEGER = re.compile(r"-?[0-9]+")
_DECIMAL = re.compile(r"-?[0-9]+(\.[0-9]+)?")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


class CastError(ValueError):
    """The value does not parse as its declared type."""


def _base(declared: str) -> str:
    return declared.split("(", 1)[0].strip().lower()


def cast(raw: str | None, declared: str, *, null_markers: tuple[str, ...] = ()):
    if raw is None or raw == "" or raw in null_markers:
        return None
    base = _base(declared)
    if base in ("character varying", "character", "text"):
        return raw
    if base in ("smallint", "integer", "bigint"):
        if not _INTEGER.fullmatch(raw):
            raise CastError(f"{raw!r} is not an integer")
        return int(raw)
    if base in ("numeric", "decimal"):
        if not _DECIMAL.fullmatch(raw):
            raise CastError(f"{raw!r} is not a decimal number")
        return decimal.Decimal(raw)
    if base == "date":
        if not _DATE.fullmatch(raw):
            raise CastError(f"{raw!r} is not a date in YYYY-MM-DD form")
        try:
            return dt.date.fromisoformat(raw)
        except ValueError as exc:
            raise CastError(f"{raw!r} is not a calendar date") from exc
    if base.startswith("timestamp"):
        try:
            value = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CastError(f"{raw!r} is not a timestamp") from exc
        if value.tzinfo is None:
            raise CastError(f"{raw!r} carries no offset, and the contract does not say its zone")
        return value.astimezone(dt.UTC)
    if base == "boolean":
        if raw in ("true", "false"):
            return raw == "true"
        raise CastError(f"{raw!r} is not true or false")
    raise CastError(f"the contract declares a type this cast does not understand: {declared!r}")


def utc_without_offset(raw: str) -> str:
    """Mark an offset-less timestamp as UTC, where a contract says the publisher means UTC."""
    if raw and len(raw) >= 19 and not raw.endswith("Z") and "+" not in raw[10:]:
        return raw + "+00:00"
    return raw
