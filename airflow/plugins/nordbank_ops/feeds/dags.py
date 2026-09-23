"""The feed DAG factory: one shape, four feeds (spec 006 section 8).

    [check key] -> [wait for file] -> discover -> open -> extract (mapped) -> register -> gate

`check key` exists only on the macro series DAG, `wait for file` only on the settlement DAG.
Every other decision is specification 005's and for the same reasons:

- **Unscheduled.** `schedule=None`, `catchup=False`, driven by explicit runs, because a run's
  logical date is the simulated business day and a wall-clock schedule has no meaning against
  it. The schedule a deployed platform would use is stated in each DAG's docstring.
- **Pools.** `open` and `register` hold `warehouse_access`; discovery, extraction and the sensor
  hold nothing, because none of them touches the warehouse. `pool` is never set in
  `default_args`, which would hand the one warehouse slot to every mapped extract task.
- **`all_done` and a gate.** One unit's failure does not stop the others from registering, and
  the run still fails, through the gate, naming what failed.
- **One asset per entity**, emitted by the register step.
"""

from __future__ import annotations

import datetime as dt

from nordbank_ops.ingest import failure_callback


def build_feed_dag(
    *,
    dag_id: str,
    system: str,
    entities: tuple[str, ...],
    discover,
    open_units,
    extract_unit,
    identifier_values=None,
    wait_for_file: dict | None = None,
    skip_reason=None,
    doc: str,
    tags: tuple[str, ...],
):
    from airflow.sdk import Asset, dag, task

    from nordbank_ops import warehouse
    from nordbank_ops.feeds import phases

    outlets = [Asset(name=f"{dag_id}/{entity}") for entity in entities]

    @dag(
        dag_id=dag_id,
        schedule=None,
        catchup=False,
        is_paused_upon_creation=False,
        start_date=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        tags=["ingest", "bronze", "feed", *tags],
        # No `pool` here: it would serialise every mapped extract task through the one slot.
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
        upstream = []

        if skip_reason is not None:

            @task.short_circuit(retries=0)
            def check_key() -> bool:
                reason = skip_reason()
                if reason:
                    print(f"skip: {reason}")
                    return False
                return True

            upstream.append(check_key())

        if wait_for_file is not None:
            from nordbank_ops.feeds.sensor import InboundFileSensor

            upstream.append(InboundFileSensor(task_id="wait_for_file", retries=0, **wait_for_file))

        @task
        def discover_deliveries(**context) -> list[dict]:
            return discover(context) if discover else []

        @task(pool=warehouse.POOL_NAME)
        def open_batches(candidates: list[dict], **context) -> list[dict]:
            return open_units(context, candidates)

        @task
        def extract(unit: dict, **context) -> list[dict]:
            reports = extract_unit(unit, context)
            context["ti"].xcom_push(key=phases.REPORT_KEY, value=reports)
            failed = [r for r in reports if r["status"] == "failed"]
            if failed:
                message = "; ".join(f"{r['entity']}: {r['failure_reason']}" for r in failed)
                # A verdict on the delivery will be the same verdict on a retry, so it fails
                # without one, as a breaking drift does at specification 005. A fetch that ran
                # out of attempts may succeed on the task's retry, so it keeps it.
                verdicts = ("breaking drift", "structurally malformed")
                if all(str(r["failure_reason"]).startswith(verdicts) for r in failed):
                    from nordbank_ops.phases import _fail_without_retrying

                    raise _fail_without_retrying(message)
                raise RuntimeError(message)
            return reports

        @task(pool=warehouse.POOL_NAME, trigger_rule="all_done", outlets=outlets)
        def register(**context) -> dict:
            return phases.register(context, system, identifier_values)

        # The gate takes the open step's allocation as well as the register step's summary. If
        # open failed, nothing was allocated, register has nothing to register, and a gate that
        # read only the summary would pass a run that did nothing; being the only leaf task, it
        # would make that run green. Requiring the allocation fails it instead.
        @task(trigger_rule="all_done", retries=0)
        def gate(summary: dict, allocated: list[dict]) -> None:
            if summary is None or allocated is None:
                raise RuntimeError(
                    "the run allocated or registered nothing because an earlier step failed; "
                    "see the failed task"
                )
            phases.gate(summary)

        found = discover_deliveries()
        for step in upstream:
            step >> found
        opened = open_batches(found)
        extracted = extract.expand(unit=opened)
        registered = register()
        extracted >> registered
        gate(registered, opened)

    return _dag()
