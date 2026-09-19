"""The `COPY` loader: dependency order, constraint policy, chunking, and sequence sync.

Everything here follows ADR 0011, which measured the choices rather than assuming them.

**`COPY` supplies the primary keys.** PostgreSQL 16 accepts explicit values into a
`generated always as identity` column under `COPY` while still refusing them through `INSERT`,
so the generator assigns every key and the schema keeps the guarantee design rule 6 was written
for. `COPY` does not advance the sequence, so `setval` afterwards is mandatory rather than
tidy-up: without it the first row the M3 mutation engine inserts collides on the primary key,
in a different milestone, with nothing here having failed.

**Foreign keys come off for the load and are revalidated after.** Measured at 63 percent of the
load time with them in place, against 1.82 seconds per million rows to revalidate, because the
parent tables are small. Secondary indexes stay: dropping them was measured at an 8 percent
saving, which does not pay for a window in which the table cannot be queried.

**The ledger commits in chunks of whole batches, and each chunk runs `ANALYZE` before it
commits.** The `ANALYZE` is load-bearing. On a freshly truncated `core.gl_entries` the planner
has no statistics and the deferred balance trigger's cached plan scans the whole table once per
row, making the commit quadratic: measured at 18 s for 20,000 entry rows, 74 s for 40,000 and
300 s for 80,000. With statistics the same commits are linear and take well under a second. A
chunk must also hold whole batches, because a batch split across two transactions is correctly
unbalanced in the first of them and the trigger rejects it.

The transport is `psql` on stdin, because the host has no PostgreSQL driver — every schema tool
in this repository reaches the database the same way. Measured at 195,000 rows per second
against a 424,000 ceiling for a file already inside the container, which is ample and avoids a
second execution environment.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from .config import RunConfig
from .spool import Spool
from .tables import IDENTITY_COLUMN, LOAD_ORDER, TABLE_COLUMNS

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import source_db_exec as db  # noqa: E402

LEDGER_ENTRIES = "gl_entries"
LEDGER_BATCHES = "gl_transactions"

# Block size for handing a spool file to psql, matching the spool's own write buffer.
COPY_BLOCK_BYTES = 1 << 20

# How often the ledger load re-analyses. The first one is load-bearing; the rest keep the
# statistics from going stale as the table grows through the chunks.
ANALYZE_EVERY = 12

# Where the dropped foreign key definitions are parked for the duration of a load.
#
# The load drops them and restores them in a `finally`, which covers an exception. It
# does not cover the process being killed, and a killed loader leaves `core` with no
# foreign keys at all: `make schema-apply` will not put them back, because the tables
# already exist and `create table if not exists` does nothing. Invariant 12 is what
# notices, and this journal is what repairs it — the next load restores from it before
# doing anything else.
JOURNAL = ROOT / "data" / "generator" / "dropped-constraints.json"


class LoadError(Exception):
    """Raised when the database refused something. The load stops rather than continuing."""


@dataclass
class LoadReport:
    counts: dict[str, int] = field(default_factory=dict)
    seconds_by_table: dict[str, float] = field(default_factory=dict)
    total_seconds: float = 0.0
    ledger_chunks: int = 0
    foreign_keys_restored: int = 0
    foreign_keys_repaired: int = 0

    @property
    def rows(self) -> int:
        return sum(self.counts.values())


def _run_psql(statements: str) -> str:
    completed = db._run(db._psql_command(False, []), stdin=statements)
    if completed.returncode != 0:
        raise LoadError((completed.stdout + completed.stderr).strip()[:2000])
    return completed.stdout


def _stream_session(
    blocks: Iterable[tuple[str, Iterator[str]]],
    *,
    preamble: str = "",
    postamble: str = "",
    label: str = "load",
) -> None:
    """Run one psql session that copies several tables, streaming each from its spool file.

    One session rather than one per table, because reaching the database means spawning a
    process inside a container and that costs about a second each time. Sixteen tables loaded
    one call at a time spent eighteen seconds on process startup and four on copying, which put
    the ci profile over its twenty second budget on overhead alone.

    stdout and stderr go to temporary files rather than pipes. psql is quiet during a copy, but
    a pipe nobody drains is a deadlock waiting for the one time it is not, and the payload here
    reaches gigabytes at the full profile.
    """
    command = db._psql_command(False, [])
    with (
        tempfile.TemporaryFile(mode="w+", encoding="utf-8") as out,
        tempfile.TemporaryFile(mode="w+", encoding="utf-8") as err,
    ):
        process = subprocess.Popen(  # noqa: S603
            command,
            stdin=subprocess.PIPE,
            stdout=out,
            stderr=err,
            text=True,
            encoding="utf-8",
            cwd=ROOT,
        )
        assert process.stdin is not None
        write = process.stdin.write
        try:
            if preamble:
                write(preamble)
            for table, lines in blocks:
                columns = ", ".join(TABLE_COLUMNS[table])
                write(f"copy core.{table} ({columns}) from stdin with (format csv);\n")
                if hasattr(lines, "read"):
                    # A spool file: hand it over in large blocks. Writing a line at a
                    # time costs one pipe write per row, which at the full profile is
                    # tens of millions of syscalls for no reason.
                    shutil.copyfileobj(lines, process.stdin, COPY_BLOCK_BYTES)
                else:
                    write("".join(lines))
                write("\\.\n")
            if postamble:
                write(postamble)
        finally:
            process.stdin.close()
        if process.wait() != 0:
            out.seek(0)
            err.seek(0)
            raise LoadError(f"{label} failed:\n{out.read()}\n{err.read()}"[:2000])


def _foreign_keys() -> list[tuple[str, str, str]]:
    """Every foreign key on a `core` table, as (table, constraint name, definition)."""
    rows = db.executor()(
        """
        select t.relname, con.conname, pg_get_constraintdef(con.oid)
          from pg_constraint con
          join pg_class t on t.oid = con.conrelid
          join pg_namespace n on n.oid = t.relnamespace
         where n.nspname = 'core' and con.contype = 'f'
         order by t.relname, con.conname
        """
    )
    return [(row[0], row[1], row[2]) for row in rows]


def _batched_entry_chunks(path: Path, chunk_rows: int) -> Iterator[list[str]]:
    """Yield chunks of `gl_entries` lines that never split a posting batch.

    A batch divided between two transactions is unbalanced in the first of them, and the
    deferred trigger is right to reject it. The chunk therefore grows past its target until the
    batch id changes.
    """
    chunk: list[str] = []
    current_batch: str | None = None
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            batch_id = line.split(",", 2)[1]
            if len(chunk) >= chunk_rows and batch_id != current_batch:
                yield chunk
                chunk = []
            current_batch = batch_id
            chunk.append(line)
    if chunk:
        yield chunk


def _write_journal(constraints: list[tuple[str, str, str]]) -> None:
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    JOURNAL.write_text(json.dumps(constraints, indent=2), encoding="utf-8")


def _clear_journal() -> None:
    JOURNAL.unlink(missing_ok=True)


def restore_dropped_constraints() -> int:
    """Put back any foreign key a previous load dropped and never restored.

    Returns the number restored. Safe to call when there is nothing to do, and safe to
    call twice: a constraint that is already present is skipped rather than re-added.
    """
    if not JOURNAL.exists():
        return 0
    journalled = [tuple(entry) for entry in json.loads(JOURNAL.read_text(encoding="utf-8"))]
    present = {name for _, name, _ in _foreign_keys()}
    missing = [entry for entry in journalled if entry[1] not in present]
    if missing:
        _run_psql(
            "".join(
                f"alter table core.{table} add constraint {name} {definition};\n"
                for table, name, definition in missing
            )
        )
    _clear_journal()
    return len(missing)


def _load_ledger_entries(path: Path, chunk_rows: int) -> tuple[int, int]:
    """Load the ledger entries as chunks of whole batches, all through one psql session.

    Each chunk is its own transaction, because the deferred balance trigger queues one event per
    row and the queue is backend memory that does not spill to disk. They share a session
    because they do not need separate ones: a process launch inside a container costs about
    0.6 s, and forty-eight of them is a minute spent on nothing.

    `ANALYZE` runs on the first chunk and then every `ANALYZE_EVERY` chunks. The first one is
    what matters and is not optional — on a freshly truncated table the planner has no
    statistics, the trigger function's cached plan scans the whole table once per row, and the
    commit becomes quadratic (ADR 0011 measured 18 s at 20,000 rows and 300 s at 80,000). It
    commits, so later chunks in later sessions plan against real statistics. The periodic repeat
    keeps them from going stale as the table grows, which costs a second and removes the
    question.
    """
    chunks = rows = 0
    columns = ", ".join(TABLE_COLUMNS[LEDGER_ENTRIES])
    command = db._psql_command(False, [])

    with (
        tempfile.TemporaryFile(mode="w+", encoding="utf-8") as out,
        tempfile.TemporaryFile(mode="w+", encoding="utf-8") as err,
    ):
        process = subprocess.Popen(  # noqa: S603
            command,
            stdin=subprocess.PIPE,
            stdout=out,
            stderr=err,
            text=True,
            encoding="utf-8",
            cwd=ROOT,
        )
        assert process.stdin is not None
        write = process.stdin.write
        try:
            # Chunks are written as they are read. Building the whole script first would hold
            # the entire ledger in memory, which is the thing the spool exists to avoid.
            for chunk in _batched_entry_chunks(path, chunk_rows):
                chunks += 1
                rows += len(chunk)
                write("begin;\n")
                write(f"copy core.{LEDGER_ENTRIES} ({columns}) from stdin with (format csv);\n")
                write("".join(chunk))
                write("\\.\n")
                if chunks == 1 or chunks % ANALYZE_EVERY == 0:
                    write(f"analyze core.{LEDGER_ENTRIES};\n")
                write("commit;\n")
        finally:
            process.stdin.close()
        if process.wait() != 0:
            out.seek(0)
            err.seek(0)
            raise LoadError(
                f"ledger load failed across {chunks} chunk(s):\n{out.read()}\n{err.read()}"[:2000]
            )
    return chunks, rows


def truncate_core() -> None:
    """Empty every `core` table. `ref` and `platform` are seeded and are not touched."""
    tables = ", ".join(f"core.{table}" for table in LOAD_ORDER)
    _run_psql(f"truncate table {tables} restart identity;\n")


def load(config: RunConfig, spool: Spool) -> LoadReport:
    report = LoadReport()
    started = time.perf_counter()
    params = config.profile.params
    chunk_rows = int(params["ledger"]["entry_rows_per_transaction"])
    drop_fks = bool(params["loading"]["drop_foreign_keys_during_load"])

    db.require_stack()

    # A previous load that was killed rather than raised leaves its constraints off. Put
    # them back before reading the catalogue, or this load would journal an already
    # incomplete set and make the gap permanent.
    repaired = restore_dropped_constraints()
    if repaired:
        report.foreign_keys_repaired = repaired

    # One session: empty the core tables and take the foreign keys off.
    constraints = _foreign_keys() if drop_fks else []
    if constraints:
        _write_journal(constraints)
    tables = ", ".join(f"core.{table}" for table in LOAD_ORDER)
    _run_psql(
        f"truncate table {tables} restart identity;\n"
        + "".join(
            f"alter table core.{table} drop constraint {name};\n" for table, name, _ in constraints
        )
    )

    handles: list = []
    try:
        # One session for every table but the ledger entries, in dependency order.
        ordinary = [table for table in LOAD_ORDER if table != LEDGER_ENTRIES]
        blocks = []
        for table in ordinary:
            path = spool.path(table)
            if not path.exists():
                report.counts[table] = 0
                continue
            handle = path.open("r", encoding="utf-8")
            handles.append(handle)
            blocks.append((table, handle))
        copy_started = time.perf_counter()
        if blocks:
            _stream_session(blocks, label="core load")
        ordinary_seconds = time.perf_counter() - copy_started
        for table, _ in blocks:
            report.seconds_by_table[table] = ordinary_seconds / len(blocks)

        # The ledger entries commit in chunks of whole batches, each chunk analysing before it
        # commits so the deferred trigger's plan uses the gl_transaction_id index. ADR 0011
        # measured the commit as quadratic without it.
        entries_path = spool.path(LEDGER_ENTRIES)
        if entries_path.exists():
            ledger_started = time.perf_counter()
            chunks, rows = _load_ledger_entries(entries_path, chunk_rows)
            report.ledger_chunks = chunks
            report.counts[LEDGER_ENTRIES] = rows
            report.seconds_by_table[LEDGER_ENTRIES] = time.perf_counter() - ledger_started
        else:
            report.counts[LEDGER_ENTRIES] = 0
    finally:
        for handle in handles:
            handle.close()
        restore = "".join(
            f"alter table core.{table} add constraint {name} {definition};\n"
            for table, name, definition in constraints
        )
        _run_psql(restore + sequence_sync_sql())
        report.foreign_keys_restored = len(constraints)
        _clear_journal()

    # The sequences were synchronised in the same session that restored the constraints.
    report.counts.update(count_rows())
    report.total_seconds = time.perf_counter() - started
    return report


def count_rows() -> dict[str, int]:
    """Row counts for all sixteen core tables, in one query rather than sixteen."""
    union = " union all ".join(
        f"select '{table}' as t, count(*) as n from core.{table}"  # noqa: S608
        for table in LOAD_ORDER
    )
    return {row[0]: int(row[1]) for row in db.executor()(f"{union} order by t")}


def sequence_sync_sql() -> str:
    """The setval statements, so a caller can fold them into a session it is already running."""
    return "".join(
        f"select setval(pg_get_serial_sequence('core.{table}', '{IDENTITY_COLUMN[table]}'), "
        f"coalesce((select max({IDENTITY_COLUMN[table]}) from core.{table}), 1), "  # noqa: S608
        f"(select count(*) > 0 from core.{table}));\n"  # noqa: S608
        for table in LOAD_ORDER
    )


def synchronise_sequences() -> None:
    """Point every identity sequence past the largest key its table holds.

    `COPY` does not advance a sequence, so without this the next identity insert collides. The
    failure is invisible until the M3 mutation engine runs, which is why `assert_sequences_ahead`
    checks the result rather than trusting it.
    """
    _run_psql(sequence_sync_sql())


def assert_sequences_ahead() -> list[str]:
    """Names of any table whose sequence has not been moved past its largest key.

    One query rather than sixteen: each round trip to the database is a process launch inside a
    container, and sixteen of them cost more than the whole ci load.
    """
    union = " union all ".join(
        f"""
        select '{table}' as t,
               coalesce(max({IDENTITY_COLUMN[table]}), 0) as max_key,
               coalesce((select last_value from pg_sequences
                          where schemaname = 'core'
                            and sequencename = substring(
                                pg_get_serial_sequence('core.{table}',
                                                       '{IDENTITY_COLUMN[table]}')
                                from 'core\\.(.*)')), 0) as last_value
          from core.{table}
        """  # noqa: S608
        for table in LOAD_ORDER
    )
    faults = []
    for row in db.executor()(f"select * from ({union}) s order by t"):
        table, max_key, last_value = row[0], int(row[1]), int(row[2])
        if max_key > 0 and last_value < max_key:
            faults.append(f"core.{table}: sequence at {last_value}, largest key {max_key}")
    return faults
