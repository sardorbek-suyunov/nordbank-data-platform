"""The ingestion DAG factory: four phases, one framework, two source schemas.

`ingest_core_banking` and `ingest_reference_data` are the same DAG over different contracts.
Reference data is not a special case — the `ref` tables carry `updated_at` and the audit
trigger like everything else, most days most of their batches are empty, and an empty
registered batch is a correct outcome that records that the entity was asked and had nothing to
say.

**Neither DAG is scheduled.** `schedule=None`, `catchup=False`, driven by explicit runs. The
source's clock is simulated, so a wall-clock schedule has no meaning against it: a day of source
data exists when a tick has written it, not when the wall clock passes midnight. Measured
before it was decided — a backfill against a paused DAG leaves its runs `queued` for ever, and
unpausing a `catchup=True` DAG with a past start date creates and runs one scheduled run per
elapsed interval at once, which would race the backfill loop and register batches for days the
tick had not produced.

A deployed platform would run `ingest_core_banking` at `0 4 * * *` UTC and
`ingest_reference_data` at `0 3 * * *`, extracting the previous day after the source's own
overnight batch has finished. That is consistent with the freshness SLA rather than merely
plausible: `metric_definitions.md` expects core banking by 06:00 UTC with a two-hour grace, so
04:00 leaves two hours for the run and the grace leaves two more for a retry.

**`pool` is never set in `default_args`.** Setting it there would hand `warehouse_access` to
every mapped extract task and serialise the whole extraction through one slot. Measured: a
mapped task with no declared pool takes `default_pool` with one slot each, and only the tasks
that declare `warehouse_access` take it.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

SOURCE_SYSTEM = "corebank"
CONTRACT_DIR = "/opt/airflow/contracts"
PROJECT_ROOT = "/opt/airflow"

EXTRACT_LAG_VARIABLE = "EXTRACT_LAG_MINUTES"
DEFAULT_EXTRACT_LAG_MINUTES = 15


def extract_lag() -> dt.timedelta:
    import os

    raw = os.environ.get(EXTRACT_LAG_VARIABLE, str(DEFAULT_EXTRACT_LAG_MINUTES))
    return dt.timedelta(minutes=int(raw))


def _contracts(source_schema: str) -> dict:
    """Every contract for one source schema, keyed on entity."""
    import sys
    from pathlib import Path

    for candidate in (PROJECT_ROOT, f"{PROJECT_ROOT}/scripts"):
        if candidate not in sys.path:
            sys.path.insert(0, candidate)

    from data_contract import load_all

    everything = load_all(Path(CONTRACT_DIR) / SOURCE_SYSTEM)
    return {
        entity: contract
        for entity, contract in everything.items()
        if contract.source_schema == source_schema
    }


def entities(source_schema: str) -> list[str]:
    return sorted(_contracts(source_schema))


def failure_callback(context: Any) -> None:
    """Write a task failure to `ops`, and never raise while doing it.

    A callback cannot acquire a pool — pools govern task scheduling, not callbacks — so this
    goes through the bounded-retry warehouse helper instead. If the warehouse is held by the
    register step at that moment the write is lost, which is acceptable for a failure record
    and is not acceptable for a second exception: a callback that fails while reporting a
    failure reports nothing and buries the original.
    """
    try:
        from nordbank_ops import warehouse

        ti = context["ti"]
        run = context["dag_run"]
        reason = str(context.get("exception") or "task failed with no exception recorded")
        with warehouse.connect(read_only=False, attempts=3, base_delay=0.5) as connection:
            connection.execute(
                """
                insert into ops.task_failure (
                    dag_id, run_id, task_id, map_index, try_number, entity, reason, failed_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    ti.dag_id,
                    run.run_id,
                    ti.task_id,
                    int(getattr(ti, "map_index", -1) or -1),
                    int(getattr(ti, "try_number", 0) or 0),
                    None,
                    reason[:2000],
                    dt.datetime.now(dt.UTC),
                ],
            )
    except Exception as exc:  # noqa: BLE001 - a reporting failure must not mask the real one
        print(f"on_failure_callback could not record the failure: {type(exc).__name__}: {exc}")


def build_ingest_dag(*, dag_id: str, source_schema: str, doc: str):
    """Build one ingestion DAG over one source schema."""
    from airflow.sdk import Asset, dag, task

    from nordbank_ops import warehouse

    known = entities(source_schema)
    outlets = [Asset(name=f"{dag_id}/{entity}") for entity in known]

    @dag(
        dag_id=dag_id,
        schedule=None,
        catchup=False,
        is_paused_upon_creation=False,
        start_date=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        tags=["ingest", "bronze", source_schema],
        # No `pool` here. See the module docstring: it would serialise every mapped extract
        # task through the one warehouse slot.
        default_args={
            "owner": "platform",
            "retries": 2,
            "retry_delay": dt.timedelta(seconds=30),
            "retry_exponential_backoff": True,
            "on_failure_callback": failure_callback,
        },
        doc_md=doc,
    )
    def _dag() -> None:
        @task(pool=warehouse.POOL_NAME)
        def open_batches(**context) -> list[dict]:
            from nordbank_ops.phases import open_phase

            return open_phase(source_schema=source_schema, context=context)

        @task
        def extract(batch: dict, **context) -> dict:
            from nordbank_ops.phases import extract_phase

            return extract_phase(source_schema=source_schema, batch=batch, context=context)

        @task(pool=warehouse.POOL_NAME, trigger_rule="all_done", outlets=outlets)
        def register(**context) -> dict:
            from nordbank_ops.phases import register_phase

            return register_phase(source_schema=source_schema, context=context)

        # `retries=0`: the gate's failure is a verdict on the run rather than a mishap. A run
        # with a failed batch will still have one on the retry, so retrying only delays the
        # answer by the backoff. `ops_source_tick` sets it to zero at M3 for the same reason.
        @task(trigger_rule="all_done", retries=0)
        def gate(summary: dict) -> None:
            from nordbank_ops.phases import gate_phase

            gate_phase(summary)

        opened = open_batches()
        extracted = extract.expand(batch=opened)
        registered = register()
        extracted >> registered
        gate(registered)

    return _dag()
