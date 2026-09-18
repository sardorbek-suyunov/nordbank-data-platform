# airflow/dags

DAG definitions, one module per DAG, module name equal to the DAG id.

DAG id naming follows `docs/conventions.md`: `ingest_<source>`, `transform_<layer>`,
`dq_<scope>`, `ops_<purpose>`, `gov_<purpose>`. Every task that touches the warehouse, read
or write, runs through the `warehouse_access` Airflow pool with a single slot, per ADR 0002.

Populated from M4.
