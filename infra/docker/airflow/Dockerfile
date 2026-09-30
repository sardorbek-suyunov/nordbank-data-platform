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

# dbt, in a virtual environment of its own (spec 007): measured, dbt-core 1.12.5 needs pathspec
# below 1.1 and Airflow 3.3.2's constraints pin 1.1.1, so the two do not resolve together. It is
# installed from the lock's export with hashes, which carries the same DuckDB as Airflow's.
COPY requirements-dbt.txt /tmp/requirements-dbt.txt
RUN python -m venv /opt/dbt \
    && /opt/dbt/bin/pip install --no-cache-dir --require-hashes -r /tmp/requirements-dbt.txt

# httpfs, installed now so that nothing downloads an extension at run time. An extension is built
# for one DuckDB version and installed under a directory named for it, which is why DuckDB is
# pinned once: Airflow's DuckDB and dbt's are the same version and both find it here. The
# directory is explicit rather than the running user's home, and read-only to that user.
ENV DUCKDB_EXTENSION_DIRECTORY=/opt/duckdb/extensions
COPY install_extensions.py /tmp/install_extensions.py
RUN /opt/dbt/bin/python /tmp/install_extensions.py \
    && chmod -R a+rX /opt/duckdb /opt/dbt \
    && /opt/dbt/bin/dbt --version
USER airflow

COPY --chown=airflow:root init.sh /opt/airflow/init.sh
