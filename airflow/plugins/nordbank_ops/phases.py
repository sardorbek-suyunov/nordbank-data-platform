"""The bodies of the four ingestion phases, outside the DAG file so they can be read and tested.

The DAG declares the shape — which task is pooled, which is mapped, what the trigger rules are
— and this holds what each one does. Keeping them apart is what lets the integration suite call
a phase directly against the running stack without going through the scheduler.

Two decisions live here rather than in the specification, and both are recorded as amendments
to it.

**`ingest_date` is the run's logical date, not the wall-clock date the batch ran on.** In this
deployment the logical date *is* the day the batch is for, because the source's clock is
simulated; in a daily deployment the two coincide. Taking the wall clock instead would collapse
a sixty-day backfill into a single partition, because all sixty runs happen on one real
afternoon, and a single partition exercises nothing that partitioning exists for. `_ingested_at`
stays the real clock reading at open time, so audit time remains real while the partition
remains logical.

**A batch whose extract task never reported is failed by the register step.** The extract task
pushes its report to XCom and then raises, so a breaking drift is both reported and a task
failure; but a task that died before pushing leaves a batch `open` with no report at all.
Register marks those failed too, with a reason that says exactly that, so no batch is left in
`open` after a run and criterion 17's "registered or explicitly failed" holds without
exception.
"""

from __future__ import annotations

import datetime as dt
import sys

REPORT_KEY = "report"
EXTRACT_TASK_ID = "extract"

for _candidate in ("/opt/airflow", "/opt/airflow/scripts"):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)


def _chains(source_schema: str) -> dict:
    """Every version of every contract for one source schema, oldest first, keyed on entity."""
    from nordbank_ops.contracts import load_chains
    from nordbank_ops.ingest import SOURCE_SYSTEM, contract_root

    everything = load_chains(contract_root() / SOURCE_SYSTEM)
    return {e: v for e, v in everything.items() if v[-1].source_schema == source_schema}


def _contract(source_schema: str, entity: str, version: int):
    """The contract a batch was opened under, which is not necessarily the one on disk."""
    from nordbank_ops.contracts import body

    return body(_chains(source_schema), entity, version)


def _interval(context: dict) -> tuple[dt.datetime, dt.datetime, dt.date]:
    """The run's interval and the source day it covers.

    Airflow 3's cron timetable carries no data interval — start, end and `run_after` are the
    same instant — so the interval is derived from the logical date rather than read off the
    run. A day is the unit this source produces.
    """
    run = context["dag_run"]
    start = run.logical_date
    if start.tzinfo is None:
        start = start.replace(tzinfo=dt.UTC)
    start = start.astimezone(dt.UTC)
    return start, start + dt.timedelta(days=1), start.date()


def open_phase(*, source_schema: str, context: dict) -> list[dict]:
    """Allocate a batch per entity in one warehouse transaction.

    The contract version is chosen here, by the interval, from `meta.contract_version`, and
    stored on the batch. Every version on disk is recorded first, so a bump committed since the
    last run is visible to the selection and a recorded version whose file was edited is
    refused before anything is allocated.
    """
    from nordbank_ops import contracts as contract_versions
    from nordbank_ops import registry, warehouse

    chains = _chains(source_schema)
    interval_start, interval_end, ingest_date = _interval(context)
    run_id = context["dag_run"].run_id
    opened_at = dt.datetime.now(dt.UTC)
    lag = _extract_lag()

    allocations: list[dict] = []
    with warehouse.connect(read_only=False) as connection:
        connection.execute("begin transaction")
        try:
            contract_versions.sync(connection, chains, opened_at)
            for entity in sorted(chains):
                current = chains[entity][-1]
                version = contract_versions.select(
                    connection, current.source_system, entity, interval_start.date()
                )
                contract = contract_versions.body(chains, entity, version)
                held = registry.watermark(connection, contract.source_system, entity)
                watermark_from = None if held is None else held - lag
                key = registry.BatchKey(
                    source_system=contract.source_system,
                    entity=entity,
                    interval_start=interval_start,
                )
                allocation = registry.allocate(
                    connection,
                    key,
                    source_schema=contract.source_schema,
                    ingest_date=ingest_date,
                    interval_end=interval_end,
                    contract_version=contract.contract_version,
                    watermark_from=watermark_from,
                    opened_at=opened_at,
                    triggering_run_id=run_id,
                )
                batch = registry.batch(connection, allocation.batch_id)
                allocations.append(
                    {
                        "entity": entity,
                        "batch_id": batch["batch_id"],
                        "contract_version": int(batch["contract_version"]),
                        "ingest_date": batch["ingest_date"].isoformat(),
                        "opened_at": batch["opened_at"].isoformat(),
                        "watermark_from": (
                            batch["watermark_from"].isoformat() if batch["watermark_from"] else None
                        ),
                        "reused": allocation.reused,
                    }
                )
            connection.execute("commit")
        except Exception:
            connection.execute("rollback")
            raise

    reused = sum(1 for a in allocations if a["reused"])
    print(f"open: {len(allocations)} batch(es) for {interval_start.date()}, {reused} reused")
    return allocations


def extract_phase(*, source_schema: str, batch: dict, context: dict) -> dict:
    """Read one entity's window and write it. No warehouse access of any kind."""
    from nordbank_ops import clients
    from nordbank_ops.extract import extract_entity
    from nordbank_ops.tokenise import Tokeniser

    contract = _contract(source_schema, batch["entity"], int(batch["contract_version"]))
    tokeniser = Tokeniser.from_environment()
    resolved = {
        "batch_id": batch["batch_id"],
        "ingest_date": dt.date.fromisoformat(batch["ingest_date"]),
        "opened_at": dt.datetime.fromisoformat(batch["opened_at"]),
        "watermark_from": (
            dt.datetime.fromisoformat(batch["watermark_from"]) if batch["watermark_from"] else None
        ),
    }

    with clients.source_cursor() as cursor:
        report = extract_entity(
            cursor=cursor,
            client=clients.lake_client(),
            bucket=clients.lake_bucket(),
            contract=contract,
            batch=resolved,
            tokeniser=tokeniser,
        )

    payload = report.as_dict()
    # Pushed before the raise, so a breaking drift is both a reported batch and a failed task.
    context["ti"].xcom_push(key=REPORT_KEY, value=payload)
    print(
        f"extract {report.entity}: read {report.rows_read}, landed {report.rows_landed}, "
        f"quarantined {report.rows_quarantined}, status {report.status}"
    )
    if report.status == "failed":
        raise _fail_without_retrying(f"{report.entity}: {report.failure_reason}")
    return payload


def _fail_without_retrying(message: str) -> Exception:
    """The exception to raise for a verdict rather than a mishap.

    A breaking drift will be breaking on the retry as well, so retrying it costs the backoff
    twice per entity per day and changes nothing. `AirflowFailException` fails the task without
    consuming a retry. The same reasoning made `ops_source_tick` set `retries: 0` at M3: a
    refusal is a state-machine answer rather than a transient failure.

    Falls back to the plain error where Airflow is not importable, so the extract phase stays
    callable from `make extract` and from a test.
    """
    from nordbank_ops.extract import BreakingDriftError

    try:
        from airflow.exceptions import AirflowFailException
    except ImportError:
        return BreakingDriftError(message)
    return AirflowFailException(message)


def register_phase(*, source_schema: str, context: dict) -> dict:
    """Register everything that wrote, fail everything that did not, in one transaction."""
    from nordbank_ops import clients, register, registry, warehouse
    from nordbank_ops.tokenise import Tokeniser

    chains = _chains(source_schema)
    interval_start, _interval_end, source_date = _interval(context)
    now = dt.datetime.now(dt.UTC)

    reports = context["ti"].xcom_pull(task_ids=EXTRACT_TASK_ID, key=REPORT_KEY) or []
    if isinstance(reports, dict):
        reports = [reports]
    reports = [r for r in reports if r]
    reported = {r["entity"] for r in reports}

    with warehouse.connect(read_only=False) as connection:
        # The contract each batch was opened under, read back from the registry, so a batch is
        # registered against the version it was validated against. The current version stands
        # in only for an entity with no batch this run, which is registered as failed.
        from nordbank_ops.contracts import body

        contracts = {entity: versions[-1] for entity, versions in chains.items()}
        for report in reports:
            opened = registry.batch(connection, report["batch_id"])
            if opened is not None:
                contracts[report["entity"]] = body(
                    chains, report["entity"], int(opened["contract_version"])
                )

        for entity in sorted(chains):
            if entity in reported:
                continue
            key = registry.BatchKey(
                source_system=contracts[entity].source_system,
                entity=entity,
                interval_start=interval_start,
            )
            for sequence, status in registry.existing_batches(connection, key):
                if status in registry.UNREGISTERED:
                    batch_id = registry.format_batch_id(entity, interval_start, sequence)
                    reports.append(
                        {
                            "entity": entity,
                            "batch_id": batch_id,
                            "status": "failed",
                            "rows_read": 0,
                            "rows_landed": 0,
                            "rows_quarantined": 0,
                            "watermark_from": None,
                            "watermark_to": None,
                            "bronze_keys": [],
                            "quarantine_keys": [],
                            "drift": [],
                            "failure_reason": "the extract task did not report",
                        }
                    )

        with clients.source_cursor() as cursor:
            summary = register.register_run(
                connection=connection,
                cursor=cursor,
                client=clients.lake_client(),
                bucket=clients.lake_bucket(),
                contracts=contracts,
                reports=sorted(reports, key=lambda r: r["entity"]),
                tokeniser=Tokeniser.from_environment(),
                source_date=source_date,
                now=now,
            )

    body = summary.as_dict()
    print(
        f"register: {len(summary.registered)} registered, {len(summary.failed)} failed, "
        f"{summary.vault_rows_added} vault row(s) added, "
        f"{summary.quarantine_rows_loaded} quarantine row(s) indexed"
    )
    for entity, reason in summary.failed:
        print(f"register: {entity} failed: {reason}")
    return body


def gate_phase(summary: dict) -> None:
    """Fail the run if any batch of it failed, naming every one."""
    failed = (summary or {}).get("failed", [])
    if not failed:
        print(f"gate: {len(summary.get('registered', []))} batch(es) registered, none failed")
        return
    lines = "; ".join(f"{item['entity']}: {item['reason']}" for item in failed)
    raise RuntimeError(f"{len(failed)} batch(es) failed and were not registered: {lines}")


def _extract_lag() -> dt.timedelta:
    from nordbank_ops.ingest import extract_lag

    return extract_lag()
