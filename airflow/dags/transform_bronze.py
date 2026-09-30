"""Build and test the bronze models whenever an ingestion registers a batch (spec 007).

One task, `dbt build --warn-error` over `models/bronze`, run as a subprocess from dbt's own
environment, inside the `warehouse_access` pool: dbt holds the warehouse file for the whole
build, so it queues behind ingestion's pooled steps like every other warehouse access (ADR 0002).

**Scheduled on the six ingestion aliases joined with `|`.** Each ingestion DAG's register step
emits one event per registered batch through its alias `<dag_id>/registered`, and an alias is
satisfied by an event on any asset it has resolved to. Joined with `|`, a registration in any
ingestion DAG triggers a run. A *list* of aliases means all of them, and measured on Airflow
3.3.2 it never fired while one alias had no events: `ingest_macro_series` registers nothing
without a FRED key, so a list would never run this DAG at all.

**Why it runs after every landing although a view needs no rebuild.** A bronze view re-lists its
prefix on every query, so a new batch is readable without dbt. The run exists for two other
reasons: it tests every landing (grain, reconciliation against the registry, tokens, object
keys), and it rebuilds a view whose entity had no landed rows when it was last built, which is a
typed empty relation until then. An asset-triggered run carries no logical date and needs none.

`max_active_runs=1`: registrations that arrive while a build runs coalesce into the next run.
`retries=0`: a failing test is a verdict on the data, not a mishap, and would fail again.
"""

from __future__ import annotations

import datetime as dt
from functools import reduce
from operator import or_

from airflow.sdk import DAG, AssetAlias, dag, task
from nordbank_ops import assets, warehouse

# Every ingestion DAG. A DAG test holds this to the `ingest_*` files in this folder, so a new
# ingestion DAG that is not listed here fails CI rather than going untransformed.
INGESTION_DAGS: tuple[str, ...] = (
    "ingest_card_settlements",
    "ingest_core_banking",
    "ingest_fx_rates",
    "ingest_macro_series",
    "ingest_reference_data",
    "ingest_sanctions_list",
)

ANY_REGISTRATION = reduce(or_, (AssetAlias(name=assets.alias_name(d)) for d in INGESTION_DAGS))


@dag(
    dag_id="transform_bronze",
    schedule=ANY_REGISTRATION,
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=False,
    start_date=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    tags=["transform", "bronze", "dbt"],
    default_args={"owner": "platform", "retries": 0},
    doc_md=__doc__,
)
def _transform_bronze() -> None:
    @task(pool=warehouse.POOL_NAME)
    def dbt_build() -> None:
        from nordbank_ops.transform import build_bronze

        build_bronze()

    dbt_build()


transform_bronze: DAG = _transform_bronze()
