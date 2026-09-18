#!/usr/bin/env bash
# One-shot initialisation for the Airflow services. Idempotent: every step is safe to repeat,
# which is what keeps metadata-database state reproducible from configuration alone (ADR 0007).
set -euo pipefail

echo "airflow-init: migrating the metadata database"
airflow db migrate

echo "airflow-init: ensuring admin user ${AIRFLOW_ADMIN_USER}"
if airflow users list --output json |
    python -c "import json,sys,os; users=json.load(sys.stdin); sys.exit(0 if any(u['username']==os.environ['AIRFLOW_ADMIN_USER'] for u in users) else 1)"; then
    echo "airflow-init: admin user already present"
else
    airflow users create \
        --username "${AIRFLOW_ADMIN_USER}" \
        --password "${AIRFLOW_ADMIN_PASSWORD}" \
        --firstname "${AIRFLOW_ADMIN_FIRSTNAME}" \
        --lastname "${AIRFLOW_ADMIN_LASTNAME}" \
        --role Admin \
        --email "${AIRFLOW_ADMIN_EMAIL}"
fi

echo "airflow-init: ensuring pool warehouse_access with one slot"
airflow pools set warehouse_access 1 "DuckDB single-access guard: every warehouse task, read or write"

echo "airflow-init: initialising the warehouse at ${DUCKDB_PATH}"
python /opt/airflow/scripts/init_warehouse.py

echo "airflow-init: done"
