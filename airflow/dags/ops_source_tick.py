"""Advance the simulated source system by one business day.

**This is not part of the data platform**, and the distinction matters enough to state rather
than leave to be inferred from a connection id. It is simulation infrastructure standing in for
the source system's own operation — the nightly batches, the customer activity and the
back-office corrections a real bank's core system would perform for itself. A real deployment
would not have it, because a real bank's core system already runs. It exists here because the
source is generated and M4's extraction needs a source that changes.

It is therefore the only thing in this repository that writes to `core`, and it authenticates
through a connection of its own, `nordbank_source_simulator`, as the application role. The name
is chosen so that a reader scanning the connection list can see which side of the line it sits
on. Every ingestion DAG from M4 onward keeps using `nordbank_source_db`, which is read-only.

**Paused by default.** M3 proves the wiring; M4 uses it to interleave ticks with extraction, and
unpausing it is that milestone's decision rather than this one's. It acquires no warehouse pool,
because it touches only the source database.

A tick is one transaction. The task either advances the simulated date by exactly one day or
leaves the source untouched, so a retry cannot half-apply a day — and `retries` is zero because
a refusal is a state-machine answer rather than a transient failure: a tick refused for being
out of order will be refused again.
"""

from __future__ import annotations

from datetime import UTC, datetime

from airflow.sdk import dag, task

# The simulation writes as the application role. Read-only extraction uses a different
# connection, and the two are never the same one.
SIMULATOR_CONN_ID = "nordbank_source_simulator"


@dag(
    dag_id="ops_source_tick",
    schedule="@daily",
    catchup=False,
    is_paused_upon_creation=True,
    start_date=datetime(2026, 1, 1, tzinfo=UTC),
    tags=["ops", "simulation"],
    default_args={"owner": "platform", "retries": 0},
    doc_md=__doc__,
)
def ops_source_tick() -> None:
    @task
    def advance_one_day() -> dict:
        """Run one tick and return what it changed, for the task log and for XCom."""
        import os
        import sys
        from pathlib import Path

        # The project root inside the image, where `generator` and `scripts` live beside the
        # DAG tree. Airflow puts the dags folder on the path and nothing else.
        root = Path("/opt/airflow")
        for candidate in (str(root), str(root / "scripts")):
            if candidate not in sys.path:
                sys.path.insert(0, candidate)

        from source_db_driver import connect

        from generator.config import load_profile
        from generator.mutation import reconcile as reconcile_module
        from generator.mutation import report as report_module
        from generator.mutation import snapshot as snapshot_module
        from generator.mutation import tick as tick_module

        settings = _settings_from_connection()
        profile = load_profile(os.environ.get("NORDBANK_ENV", "dev"))

        with connect(settings) as connection:
            report = tick_module.run(connection, requested_date=None, profile=profile)
            with connection.cursor() as cursor:
                reconciliation = reconcile_module.reconcile_latest(cursor)
            connection.rollback()
            reconcile_module.assert_agrees(reconciliation)

            with connection.cursor() as cursor:
                snapshot_module.synchronise_sequences(cursor)
            connection.commit()

        print(report_module.format_report(report))
        print(reconcile_module.format_reconciliation(reconciliation))
        return {
            "simulated_date": report.simulated_date.isoformat(),
            "tick_sequence": report.tick_sequence,
            "rows_touched": report.rows_touched,
            "duration_ms": report.duration_ms,
            "drift_fired": report.drift_fired,
        }

    advance_one_day()


def _settings_from_connection():
    """The simulator connection, as the driver's own settings object.

    Read from Airflow rather than from `.env`, because inside the stack the source is the
    compose service name and from the host it is localhost on the published port. The driver
    resolves the second; this resolves the first.
    """
    import sys

    for candidate in ("/opt/airflow", "/opt/airflow/scripts"):
        if candidate not in sys.path:
            sys.path.insert(0, candidate)

    from nordbank_ops import clients
    from source_db_driver import Settings

    conn = clients._connection(SIMULATOR_CONN_ID)  # noqa: SLF001 - the plugin's own accessor
    return Settings(
        host=conn.host,
        port=int(conn.port or 5432),
        dbname=conn.schema or "nordbank",
        user=conn.login,
        password=conn.password,
    )


ops_source_tick()
