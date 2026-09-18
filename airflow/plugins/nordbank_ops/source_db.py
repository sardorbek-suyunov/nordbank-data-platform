"""Assertions about the source database, expressed against a DB-API cursor.

The functions take a cursor rather than a connection string so that they can be tested with a
fake, and so that the caller owns connection lifetime.
"""

from __future__ import annotations

from typing import Any

REQUIRED_SCHEMAS = ("core", "ref")


def schema_names(cursor: Any) -> set[str]:
    cursor.execute("select schema_name from information_schema.schemata")
    return {row[0] for row in cursor.fetchall()}


def assert_schemas(cursor: Any, required: tuple[str, ...] = REQUIRED_SCHEMAS) -> list[str]:
    present = schema_names(cursor)
    missing = [schema for schema in required if schema not in present]
    if missing:
        raise AssertionError(f"source database is missing schemas: {missing}")
    return list(required)


def current_user(cursor: Any) -> str:
    cursor.execute("select current_user")
    return cursor.fetchone()[0]


def assert_cannot_create(cursor: Any, schema: str = "core") -> str:
    """The extraction role must be refused a CREATE TABLE. Returns the SQLSTATE seen.

    Raises AssertionError if the statement succeeds, which would mean extraction credentials
    can write to the source system.
    """
    try:
        cursor.execute(f"create table {schema}.nordbank_privilege_probe (id integer)")
    except Exception as exc:
        sqlstate = getattr(exc, "pgcode", None) or getattr(exc, "sqlstate", None)
        return str(sqlstate) if sqlstate else type(exc).__name__
    raise AssertionError(f"extraction role created a table in {schema}; it must hold SELECT only")
