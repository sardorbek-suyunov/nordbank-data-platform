"""Run SQL against postgres-source from the host, through psql inside the container.

The host has no Postgres driver and does not need one: the database is in a container that
already ships psql. Every host-side schema target goes through here, so there is one place that
knows how to reach the source database and one place that turns psql output back into rows.

Inside the stack the same queries run through a psycopg2 cursor instead. That is the point of
the executor being a callable: `scripts/schema_contract.py` does not know or care which it got.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from env_file import read_dotenv  # noqa: E402

# A psql field separator has to be something no catalogue value can contain, and the obvious
# candidate, the vertical bar, appears in check constraint text.
FIELD_SEPARATOR = "\x1f"

REQUIRED = (
    "POSTGRES_SOURCE_DB",
    "SOURCE_APP_USER",
    "SOURCE_APP_PASSWORD",
    "SOURCE_READ_USER",
    "SOURCE_READ_PASSWORD",
)


@lru_cache(maxsize=1)
def settings() -> tuple[tuple[str, str], ...]:
    values = read_dotenv(ROOT / ".env")
    missing = [key for key in REQUIRED if not values.get(key)]
    if missing:
        raise SystemExit(
            f"schema tooling: .env is missing {', '.join(missing)}; run `make init-env`"
        )
    return tuple((key, values[key]) for key in REQUIRED)


def _setting(key: str) -> str:
    return dict(settings())[key]


def _credentials(as_reader: bool) -> tuple[str, str]:
    if as_reader:
        return _setting("SOURCE_READ_USER"), _setting("SOURCE_READ_PASSWORD")
    return _setting("SOURCE_APP_USER"), _setting("SOURCE_APP_PASSWORD")


def _psql_command(as_reader: bool, extra: Iterable[str]) -> list[str]:
    user, password = _credentials(as_reader)
    return [
        "docker",
        "compose",
        "exec",
        "-T",
        "-e",
        f"PGPASSWORD={password}",
        # 60_grants.sql reads this rather than hard-coding a role name that .env may rename.
        "-e",
        f"PGOPTIONS=-c nordbank.read_user={_setting('SOURCE_READ_USER')}",
        "postgres-source",
        "psql",
        "-X",
        "-q",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        user,
        "-d",
        _setting("POSTGRES_SOURCE_DB"),
        *extra,
    ]


def _run(command: list[str], stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, input=stdin, capture_output=True, text=True, cwd=ROOT, check=False
    )


def run_sql_file(container_path: str) -> None:
    """Apply one SQL file that is mounted inside the postgres-source container."""
    completed = _run(_psql_command(False, ["-f", container_path]))
    if completed.returncode != 0:
        sys.stderr.write(completed.stdout)
        sys.stderr.write(completed.stderr)
        raise SystemExit(f"schema tooling: {container_path} failed")
    if completed.stdout.strip():
        print(completed.stdout.rstrip())


def run_sql(sql: str, *, as_reader: bool = False) -> str:
    """Run SQL from stdin and return raw stdout."""
    completed = _run(_psql_command(as_reader, []), stdin=sql)
    if completed.returncode != 0:
        sys.stderr.write(completed.stdout)
        sys.stderr.write(completed.stderr)
        raise SystemExit("schema tooling: statement failed")
    return completed.stdout


def executor(*, as_reader: bool = False):
    """A `schema_contract.Executor` backed by psql."""

    def execute(sql: str) -> list[tuple[str, ...]]:
        command = _psql_command(as_reader, ["-A", "-t", "-F", FIELD_SEPARATOR, "-c", sql])
        completed = _run(command)
        if completed.returncode != 0:
            sys.stderr.write(completed.stdout)
            sys.stderr.write(completed.stderr)
            raise SystemExit("schema tooling: query failed")
        return [
            tuple(line.split(FIELD_SEPARATOR))
            for line in completed.stdout.splitlines()
            if line.strip()
        ]

    return execute


def require_stack() -> None:
    completed = _run(["docker", "compose", "ps", "--status", "running", "--services"])
    if "postgres-source" not in completed.stdout.split():
        raise SystemExit(
            "schema tooling: postgres-source is not running. Start the stack with `make up`."
        )


def quote_literal(value: str) -> str:
    """Single-quote a string for SQL, doubling embedded quotes."""
    return "'" + value.replace("'", "''") + "'"
