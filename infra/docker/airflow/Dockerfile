FROM apache/airflow:3.3.2-python3.11

ARG AIRFLOW_VERSION=3.3.2
ARG PYTHON_VERSION=3.11
ARG CONSTRAINTS_URL=https://raw.githubusercontent.com/apache/airflow/constraints-${AIRFLOW_VERSION}/constraints-${PYTHON_VERSION}.txt

COPY requirements.txt requirements-warehouse.txt /tmp/

RUN pip install --no-cache-dir --constraint "${CONSTRAINTS_URL}" -r /tmp/requirements.txt

# DuckDB, pinned once in the lock and exported with its hashes. It has no dependencies, so it is
# installed on its own, where hash checking cannot collide with the constrained install above.
RUN pip install --no-cache-dir --no-deps --require-hashes -r /tmp/requirements-warehouse.txt

RUN python -c "import duckdb, pyarrow, boto3, psycopg2, requests, pytest; \
import airflow.providers.fab as fab; \
print('duckdb', duckdb.__version__); \
print('fab provider', fab.__file__)"

USER root
RUN mkdir -p /opt/warehouse /opt/airflow/exports \
    && chown -R airflow:root /opt/warehouse /opt/airflow/exports
USER airflow

COPY --chown=airflow:root init.sh /opt/airflow/init.sh
