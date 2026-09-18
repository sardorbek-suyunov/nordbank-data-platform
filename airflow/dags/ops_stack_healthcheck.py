"""Stack health check.

One DAG that touches every connection the platform has: the source database as the extraction
role, the lake bucket including the batch-id key property, and the warehouse in both
directions. Triggered by hand or by CI; it is the end-to-end proof that a stack is usable.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from airflow.sdk import dag, task
from nordbank_ops import clients, lake, source_db, warehouse


@dag(
    dag_id="ops_stack_healthcheck",
    schedule=None,
    catchup=False,
    start_date=datetime(2026, 1, 1, tzinfo=UTC),
    tags=["ops"],
    default_args={"owner": "platform", "retries": 0},
    doc_md=__doc__,
)
def ops_stack_healthcheck() -> None:
    @task
    def check_source_database() -> list[str]:
        with clients.source_cursor() as cursor:
            user = source_db.current_user(cursor)
            schemas = source_db.assert_schemas(cursor)
        print(f"source database reachable as {user}, schemas present: {schemas}")
        return schemas

    @task
    def check_lake() -> dict:
        client = clients.lake_client()
        bucket = clients.lake_bucket()
        lake.assert_prefixes(client, bucket)

        run = uuid.uuid4().hex[:8]
        result = lake.probe_bucket(
            client,
            bucket,
            (f"{run}a", f"{run}b"),
            datetime.now(UTC).date().isoformat(),
        )
        print(f"lake probe wrote two distinct keys under one ingest date: {result['keys']}")
        return result

    @task(pool=warehouse.POOL_NAME)
    def write_warehouse_probe() -> str:
        probe_id = uuid.uuid4().hex
        with warehouse.connect(read_only=False) as connection:
            missing = warehouse.missing_schemas(connection)
            if missing:
                raise AssertionError(f"warehouse is missing schemas: {missing}")
            connection.execute(
                f"insert into {warehouse.PROBE_TABLE} values (?, ?, ?, ?)",
                [probe_id, datetime.now(UTC), "stack_healthcheck", "written read-write"],
            )
        print(f"warehouse probe written: {probe_id}")
        return probe_id

    @task(pool=warehouse.POOL_NAME)
    def read_warehouse_probe(probe_id: str) -> str:
        with warehouse.connect(read_only=True) as connection:
            row = connection.execute(
                f"select probe_id, component, detail from {warehouse.PROBE_TABLE} "
                "where probe_id = ?",
                [probe_id],
            ).fetchone()
        if row is None:
            raise AssertionError(f"probe {probe_id} was not readable through a read-only open")
        print(f"warehouse probe read back through a read-only connection: {row}")
        return row[0]

    check_source_database()
    check_lake()
    read_warehouse_probe(write_warehouse_probe())


ops_stack_healthcheck()
