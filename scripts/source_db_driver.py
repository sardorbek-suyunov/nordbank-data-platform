"""Reach postgres-source from the host with a driver, for work that needs a transaction.

`source_db_exec.py` reaches the same database through `psql` inside the container, and it stays:
`make schema-apply` runs SQL files that are mounted inside that container, which a host driver
cannot open. This module is the other half, and the division is deliberate — the driver is for
the mutation engine, psql is for anything that applies a file from inside the container.

**Why a driver at all.** M2 recorded "the host has no Postgres driver" as a constraint when it
was a dependency choice, and M3 is the milestone where the difference is load-bearing, so it
was measured. On the workload a tick performs — read 17,907 rows of state, then one transaction
copying 10,403 rows and folding 1,824 balances — psycopg reads state in 31 to 40 ms against
298 to 386 ms through a held-open psql session, and writes in 464 to 514 ms against 803 to
861 ms one-shot. It also gives a real `rollback()`, which spec 004's acceptance criterion 2 is
written in terms of, instead of a sentinel protocol over a pipe whose failure mode is a hang.
Recorded in ADR 0012.

**Why the identity assertion exists.** The driver depends on the published port reaching the
container, and on the machine this was built on it did not: `docker compose ps` reported
`0.0.0.0:5432->5432/tcp` while a native PostgreSQL service owned the port, the stack came up
healthy, and the driver silently reached the wrong server. Nothing in Docker says this has
happened, so the connection proves where it landed rather than assuming it, and every failure
path names the collision as a candidate.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from env_file import read_dotenv  # noqa: E402

# The major version the schema was written and measured against. A different major is not
# necessarily broken, but it is not what anything here was validated on, so it is reported.
EXPECTED_MAJOR = 16
EXPECTED_SCHEMAS = ("core", "ref", "platform")

CONNECT_TIMEOUT = 10


# The custom setting core.set_updated_at() reads, per spec 002 design rule 4 as amended.
SIMULATION_CLOCK = "nordbank.sim_now"

# `SET LOCAL <name> = %s` is a syntax error: SET takes no bind parameter. set_config with
# is_local true is the function form of SET LOCAL and does, which keeps the value parameterised
# rather than interpolated into SQL. Its second argument is text, so the instant is rendered
# here rather than left to the driver's timestamptz adaptation, which set_config will not
# accept.
_SET_CLOCK = "select set_config(%s, %s, true)"


class SourceDatabaseError(RuntimeError):
    """The source database could not be reached, or what answered was not the source database."""


@dataclass(frozen=True)
class Settings:
    """Everything needed to open a connection, from wherever the caller resolved it."""

    host: str
    port: int
    dbname: str
    user: str
    password: str

    @property
    def where(self) -> str:
        return f"{self.host}:{self.port}/{self.dbname}"

    def kwargs(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "dbname": self.dbname,
            "user": self.user,
            "password": self.password,
            "connect_timeout": CONNECT_TIMEOUT,
        }


def resolve(env_path: Path | None = None) -> Settings:
    """Connection settings for the application role.

    The environment wins over the file, so a container can point at the compose service name
    without a second copy of the credentials: inside the stack the database is
    `postgres-source:5432`, and from the host it is localhost on the published port.
    """
    values = read_dotenv(env_path or ROOT / ".env")
    missing = [
        key
        for key in ("POSTGRES_SOURCE_DB", "SOURCE_APP_USER", "SOURCE_APP_PASSWORD")
        if not (os.environ.get(key) or values.get(key))
    ]
    if missing:
        raise SourceDatabaseError(
            f"cannot reach the source database: {', '.join(missing)} is not set in the "
            f"environment or in .env. Run `make init-env`."
        )

    def setting(key: str, default: str = "") -> str:
        return os.environ.get(key) or values.get(key) or default

    return Settings(
        host=setting("NORDBANK_SOURCE_HOST", "localhost"),
        port=int(setting("NORDBANK_SOURCE_PORT") or setting("POSTGRES_SOURCE_PORT", "5432")),
        dbname=setting("POSTGRES_SOURCE_DB"),
        user=setting("SOURCE_APP_USER"),
        password=setting("SOURCE_APP_PASSWORD"),
    )


def _port_collision_hint(settings: Settings) -> str:
    if settings.host not in ("localhost", "127.0.0.1", "::1"):
        return ""
    return (
        f"\n  Another PostgreSQL may be bound to port {settings.port} on this machine, in which "
        f"case the container's published port is shadowed and Docker does not say so. Check "
        f"`docker compose ps postgres-source`, then set POSTGRES_SOURCE_PORT in .env to a free "
        f"port and run `make down && make up`."
    )


def assert_identity(connection: Any, settings: Settings) -> None:
    """Prove the connection landed on the Nordbank source rather than on some other server."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            select current_setting('server_version_num')::int / 10000 as major,
                   (select count(*) from information_schema.schemata
                      where schema_name = any(%s)) as schemas
            """,
            (list(EXPECTED_SCHEMAS),),
        )
        major, schemas = cursor.fetchone()

    if schemas != len(EXPECTED_SCHEMAS):
        raise SourceDatabaseError(
            f"connected to a PostgreSQL at {settings.where}, but it is not the Nordbank source: "
            f"{schemas} of {len(EXPECTED_SCHEMAS)} expected schemas "
            f"({', '.join(EXPECTED_SCHEMAS)}) are present."
            + _port_collision_hint(settings)
            + "\n  If it is the right database, run `make schema-apply`."
        )
    if major != EXPECTED_MAJOR:
        print(
            f"source database: server is PostgreSQL {major}, and this platform was measured "
            f"against {EXPECTED_MAJOR}",
            file=sys.stderr,
        )


@contextmanager
def connect(settings: Settings | None = None, *, autocommit: bool = False) -> Iterator[Any]:
    """Open a connection to the source database and prove it is the right one.

    The caller owns the transaction. Nothing here commits: a tick is one transaction and the
    decision to keep it belongs to the code that knows whether it succeeded.
    """
    import psycopg

    resolved = settings or resolve()
    try:
        connection = psycopg.connect(**resolved.kwargs(), autocommit=autocommit)
    except psycopg.OperationalError as error:
        text = str(error)
        if "authentication failed" in text or "password" in text.lower():
            raise SourceDatabaseError(
                f"a PostgreSQL answered at {resolved.where} but refused the credentials for "
                f"{resolved.user!r}." + _port_collision_hint(resolved)
            ) from error
        raise SourceDatabaseError(
            f"could not reach the source database at {resolved.where}. Is the stack running? "
            f"Start it with `make up`.\n  {text.strip().splitlines()[0]}"
        ) from error

    try:
        assert_identity(connection, resolved)
        if not autocommit:
            # The identity query opened a transaction. The caller's work should start in a
            # fresh one, so that its rollback cannot be confused with this.
            connection.rollback()
        yield connection
    finally:
        connection.close()


def set_simulation_clock(cursor: Any, moment: Any) -> None:
    """Point the `updated_at` trigger at a simulated instant for the rest of this transaction.

    Transaction-scoped, so it is discarded at commit and at rollback alike and cannot reach
    another session or a later transaction on a pooled connection. A plain `SET` would survive
    the commit and backdate everything written afterwards on that connection.
    """
    cursor.execute(_SET_CLOCK, (SIMULATION_CLOCK, moment.isoformat()))


def clear_simulation_clock(cursor: Any) -> None:
    """Return the trigger to the wall clock for the rest of this transaction.

    `core` and `ref` carry simulated time; `platform` carries real time, because
    `platform.tick_log` is a reconciliation control M7 reads and a control that lies about when
    it ran is useless. Clearing is not scoped to the next statement: it holds until the
    transaction ends. So this is an ordering requirement rather than a toggle — set once, write
    `core` and `ref`, clear once, write `platform` last.
    """
    cursor.execute(_SET_CLOCK, (SIMULATION_CLOCK, ""))
