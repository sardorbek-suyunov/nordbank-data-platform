"""The scripted schema drift a tick fires.

Last of the phases, and last for a reason: every row written earlier in the tick was written
against one schema. A drift event that ran first would leave the tick's own inserts straddling a
shape change, which is not what a source system does — a migration runs in its own window.

**PostgreSQL DDL is transactional, and this depends on it.** Verified inside a tick's
transaction: the added column was present, and after a rollback it was gone. So a half-applied
drift event — the DDL without the log row, or the log row without the DDL — is not a state the
source can reach.

**The log row and the classification row are written after the simulation clock is cleared**,
with everything else in `platform`. `platform.drift_log` is a control M4 and M7 read and a
control that lies about when it ran is useless, and `platform.column_classifications` is the
table the extraction layer reads to decide what to tokenise. The DDL itself carries no
timestamp, so nothing is lost by splitting them across the clock: they are still one
transaction, which is what spec 004's amendment requires.
"""

from __future__ import annotations

from ... import drift as drift_module
from ..tick import TickContext


def run(context: TickContext) -> None:
    """Fire every event whose simulated date has arrived and which has not fired yet."""
    already = set(drift_module.fired_names(_executor(context)))
    due = drift_module.due_events(context.snapshot.anchor_date, context.simulated_date, already)
    for event in due:
        context.cursor.execute(event.apply_sql)
        context.report.drift_fired.append(event.name)


def _executor(context: TickContext):
    def run_query(sql: str) -> list[tuple]:
        context.cursor.execute(sql)
        return context.cursor.fetchall()

    return run_query
