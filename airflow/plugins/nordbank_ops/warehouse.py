"""Single point of access to the DuckDB warehouse file.

DuckDB allows one process to hold a database file, and a read-write holder excludes read-only
openers as well as other writers (ADR 0002). Airflow serialises its own tasks through the
`warehouse_access` pool; this module adds bounded retry for the access the pool does not
govern, such as a health probe run from outside Airflow.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

POOL_NAME = "warehouse_access"
SCHEMAS = ("bronze", "silver", "gold", "dq", "ops", "meta")
PROBE_TABLE = "ops.stack_health_probe"

LOCK_MARKERS = (
    "conflicting lock",
    "could not set lock",
    "being used by another process",
)
_PID = re.compile(r"pid[\s:]*(\d+)", re.IGNORECASE)


class WarehouseBusyError(RuntimeError):
    """Raised when the warehouse file stayed locked for the whole retry budget."""


def warehouse_path() -> Path:
    raw = os.environ.get("DUCKDB_PATH")
    if not raw:
        raise RuntimeError("DUCKDB_PATH is not set; the warehouse path comes from the environment")
    return Path(raw)


def is_lock_conflict(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in LOCK_MARKERS)


def lock_holder(exc: BaseException) -> str | None:
    """The pid DuckDB names in its lock error, where it names one."""
    match = _PID.search(str(exc))
    return match.group(1) if match else None


def _duckdb_opener(path: str, read_only: bool) -> Any:
    import duckdb

    return duckdb.connect(path, read_only=read_only)


@contextmanager
def connect(
    *,
    read_only: bool = False,
    attempts: int = 5,
    base_delay: float = 0.5,
    path: Path | str | None = None,
    opener: Callable[[str, bool], Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[Any]:
    """Open the warehouse, retrying with exponential backoff while it is locked."""
    open_db = opener or _duckdb_opener
    target = str(path if path is not None else warehouse_path())
    last_error: BaseException | None = None

    for attempt in range(1, attempts + 1):
        try:
            connection = open_db(target, read_only)
        except Exception as exc:
            if not is_lock_conflict(exc):
                raise
            last_error = exc
            if attempt < attempts:
                sleep(base_delay * 2 ** (attempt - 1))
            continue

        try:
            yield connection
        finally:
            connection.close()
        return

    holder = lock_holder(last_error) if last_error else None
    held_by = f" (held by pid {holder})" if holder else ""
    raise WarehouseBusyError(
        f"warehouse at {target} is locked by another process{held_by}; "
        f"gave up after {attempts} attempts"
    ) from last_error


def initialise(connection: Any) -> None:
    """Create the six schemas and the health probe table. Safe to run repeatedly."""
    for schema in SCHEMAS:
        connection.execute(f"create schema if not exists {schema}")
    connection.execute(
        f"""
        create table if not exists {PROBE_TABLE} (
            probe_id varchar not null,
            probed_at timestamptz not null,
            component varchar not null,
            detail varchar
        )
        """
    )


def schema_names(connection: Any) -> set[str]:
    rows = connection.execute("select schema_name from information_schema.schemata").fetchall()
    return {row[0] for row in rows}


def missing_schemas(connection: Any) -> list[str]:
    present = schema_names(connection)
    return [schema for schema in SCHEMAS if schema not in present]
