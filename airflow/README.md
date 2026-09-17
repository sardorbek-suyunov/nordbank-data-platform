# airflow

Airflow home for the platform: DAGs, plugins and their tests. Airflow 3.x with asset-driven
scheduling.

Owns orchestration only. Transformation logic belongs to dbt and extraction logic to
`generator/` and `scripts/`; DAGs call those, they do not reimplement them.

The Airflow image, service configuration and ports live in `infra/docker/`.
