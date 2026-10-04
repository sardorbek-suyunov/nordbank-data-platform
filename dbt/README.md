# dbt

dbt project for the warehouse: bronze, silver and gold models, snapshots, macros, tests and
seeds.

Owns everything inside the warehouse from the bronze schema upward. Landing data in the lake
and registering each batch is Airflow's responsibility; bronze is a view per entity over the
lake that reads only registered batches, and dbt owns those views (specification 007).

One profile, `profiles.yml`, with one target, `warehouse`, reading everything from the
environment. dbt runs in its own virtual environment in the image, as a subprocess
(`airflow/plugins/nordbank_ops/transform.py`): from `transform_bronze` and `transform_silver`
inside the `warehouse_access` pool, and from `make dbt-build`. The project is mounted read-only; dbt writes
its target and logs under `/tmp/dbt` in the container.

Primary target is DuckDB; BigQuery is kept as a secondary target to prove portability
(ADR 0002), and M10 decides how bronze is expressed there.

`quasi_identifiers_in_gold.yml`, beside the project file and not parsed by dbt, declares the
quasi-identifier columns permitted in gold and the generalisation each reaches gold as.
`make silver-generate` writes each into the column's properties, where the gold column guard reads
it; M8's governance check reads the file itself.
