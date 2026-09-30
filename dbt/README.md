# dbt

dbt project for the warehouse: bronze, silver and gold models, snapshots, macros, tests and
seeds.

Owns everything inside the warehouse from the bronze schema upward. Landing data in the lake
and registering each batch is Airflow's responsibility; bronze is a view per entity over the
lake that reads only registered batches, and dbt owns those views (specification 007).

One profile, `profiles.yml`, with one target, `warehouse`, reading everything from the
environment. dbt runs in its own virtual environment in the image, as a subprocess
(`airflow/plugins/nordbank_ops/transform.py`): from `transform_bronze` inside the
`warehouse_access` pool, and from `make dbt-build`. The project is mounted read-only; dbt writes
its target and logs under `/tmp/dbt` in the container.

Primary target is DuckDB; BigQuery is kept as a secondary target to prove portability
(ADR 0002), and M10 decides how bronze is expressed there.
