# dbt

dbt project for the warehouse: bronze, silver and gold models, snapshots, macros, tests and
seeds.

Owns everything inside the warehouse from the bronze schema upward. Landing raw data into
the lake and loading it into bronze tables is Airflow's responsibility, not dbt's.

Primary target is DuckDB; BigQuery is kept as a secondary target to prove portability
(ADR 0002). The dbt project itself is initialised at M4; M0 creates the directory layout only.
