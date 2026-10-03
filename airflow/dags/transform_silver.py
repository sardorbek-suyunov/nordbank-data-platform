"""Build and test silver whenever bronze has been built (specification 008 section 10).

One task, `dbt build --warn-error` over the seed and `models/silver`, run as a subprocess from
dbt's own environment, inside the `warehouse_access` pool: dbt holds the warehouse file for the
whole build, so it queues behind ingestion's pooled steps like every other warehouse access
(ADR 0002). Silver is tables rebuilt in full; incremental is M6's decision.

**Scheduled on `transform_bronze/built`,** the asset `transform_bronze`'s build declares as its
outlet, which Airflow emits when that build succeeds. So silver is built after every successful
bronze build and never from a bronze that failed its tests, and a backfill, which builds bronze
once at the end, builds silver once after it and waits for it.

`max_active_runs=1`: bronze builds that finish while silver builds coalesce into the next run.
`retries=0`: a failing test is a verdict on the data, not a mishap, and would fail again.
"""

from __future__ import annotations

import datetime as dt

from airflow.sdk import DAG, Asset, dag, task
from nordbank_ops import transform, warehouse


@dag(
    dag_id="transform_silver",
    schedule=Asset(name=transform.BRONZE_BUILT),
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=False,
    start_date=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    tags=["transform", "silver", "dbt"],
    default_args={"owner": "platform", "retries": 0},
    doc_md=__doc__,
)
def _transform_silver() -> None:
    @task(pool=warehouse.POOL_NAME)
    def dbt_build() -> None:
        transform.build_silver()

    dbt_build()


transform_silver: DAG = _transform_silver()
