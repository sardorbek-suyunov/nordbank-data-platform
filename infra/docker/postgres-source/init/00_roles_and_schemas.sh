#!/bin/bash
# Applied by the Postgres entrypoint on first start only, as the superuser.
# Creates the two roles and the two schemas the platform expects. No business tables: DDL is M2.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER}" --dbname "${POSTGRES_DB}" <<-SQL
    alter database ${POSTGRES_DB} set timezone to 'UTC';

    create role ${SOURCE_APP_USER} login password '${SOURCE_APP_PASSWORD}';
    create role ${SOURCE_READ_USER} login password '${SOURCE_READ_PASSWORD}';

    create schema core authorization ${SOURCE_APP_USER};
    create schema ref authorization ${SOURCE_APP_USER};
SQL

echo "postgres-source: roles ${SOURCE_APP_USER} and ${SOURCE_READ_USER} created, schemas core and ref created"
