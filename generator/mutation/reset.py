"""What `make seed` has to undo before it loads a new book.

Spec 004 section 7 says `seed` resets `platform.simulation_state` to the anchor date. It has to
reset more than that, and the reason is a state the source could otherwise reach and not
recover from.

A schema that is post-drift while `platform.drift_log` says nothing has fired is incoherent.
`make schema-check` computes the expected schema as the committed dictionary plus the deltas of
the events the log says fired, so with an empty log it would expect the pre-drift shape and
find the post-drift one — and the failure would surface as an undocumented column, far from the
seed that caused it.

So a seed reverts every fired event, removes the classification rows those events added, empties
the tick log and the drift log, and points the simulation back at the anchor. Every drift event
declares its reversal alongside its application and both are idempotent, so reverting an event
that was never applied is a no-op rather than an error.

This runs through `psql`, like the rest of the load. The rule in ADR 0012 is that the driver is
for the tick and `psql` is for anything the load does, and a seed is the load.
"""

from __future__ import annotations

import datetime as dt
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import source_db_exec as db  # noqa: E402

from .. import drift as drift_module  # noqa: E402

# Emptied in one statement, because `tick_table_counts` and `tick_deleted_keys` reference
# `tick_log` and Postgres refuses to truncate a referenced table on its own even when the child
# is already empty. Naming all four is the alternative to `cascade`, which would also silently
# empty anything else that came to reference them.
TICK_TABLES = (
    "platform.tick_deleted_keys",
    "platform.tick_table_counts",
    "platform.tick_log",
    "platform.drift_log",
)


@dataclass
class ResetReport:
    """What the reset undid, for the seed's output."""

    reverted: list[str] = field(default_factory=list)
    ticks_cleared: int = 0
    anchor: dt.date | None = None

    @property
    def changed_the_schema(self) -> bool:
        return bool(self.reverted)


def reset(*, profile: str, seed: int, anchor: dt.date) -> ResetReport:
    """Revert fired drift, empty the logs, and point the simulation at the anchor."""
    execute = db.executor()
    report = ResetReport(anchor=anchor)

    rows = execute("select count(*) from platform.tick_log")
    report.ticks_cleared = int(rows[0][0]) if rows else 0

    fired = drift_module.fired_names(execute)
    statements: list[str] = []

    for name in reversed(fired):
        event = drift_module.event_by_name(name)
        if event is None:
            continue
        statements.append(event.revert_sql + ";")
        if event.added is not None:
            statements.append(
                "delete from platform.column_classifications "
                f"where schema_name = {db.quote_literal(event.added.schema)} "
                f"and table_name = {db.quote_literal(event.added.table)} "
                f"and column_name = {db.quote_literal(event.added.name)};"
            )
        report.reverted.append(name)

    statements.append(f"truncate table {', '.join(TICK_TABLES)};")
    statements.append(_simulation_state_sql(profile=profile, seed=seed, anchor=anchor))

    db.run_sql("begin;\n" + "\n".join(statements) + "\ncommit;\n")
    return report


def _simulation_state_sql(*, profile: str, seed: int, anchor: dt.date) -> str:
    """Point the singleton at the anchor, creating it when the load is the first."""
    return f"""
insert into platform.simulation_state
    (profile, seed, anchor_date, simulated_date, tick_sequence, last_tick_completed_at)
values ({db.quote_literal(profile)}, {int(seed)}, date {db.quote_literal(anchor.isoformat())},
        date {db.quote_literal(anchor.isoformat())}, 0, null)
-- Inferred from the expression index that makes this table a singleton. `on conflict on
-- constraint` cannot be used: a bare unique index is not a constraint.
on conflict ((true)) do update
   set profile                = excluded.profile,
       seed                   = excluded.seed,
       anchor_date            = excluded.anchor_date,
       simulated_date         = excluded.simulated_date,
       tick_sequence          = 0,
       last_tick_completed_at = null;
"""
