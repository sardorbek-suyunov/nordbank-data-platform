#!/bin/bash
# Privileges for the extraction role. It reads and nothing else, including tables that do not
# exist yet, which is what the default privileges are for. Extraction never authenticates as an
# owner.
#
# The application role is also granted CREATE on the database. It owns core and ref, which this
# script's companion creates, and it creates and owns the platform schema itself in the numbered
# DDL (spec 002 section 1a). Without this grant that CREATE SCHEMA is refused, because CREATE on
# a database is not held by PUBLIC. This is the only privilege the application role needs beyond
# ownership of what it creates.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER}" --dbname "${POSTGRES_DB}" <<-SQL
    grant connect on database ${POSTGRES_DB} to ${SOURCE_READ_USER};
    grant usage on schema core, ref to ${SOURCE_READ_USER};
    grant select on all tables in schema core, ref to ${SOURCE_READ_USER};

    alter default privileges for role ${SOURCE_APP_USER} in schema core
        grant select on tables to ${SOURCE_READ_USER};
    alter default privileges for role ${SOURCE_APP_USER} in schema ref
        grant select on tables to ${SOURCE_READ_USER};

    grant create on database ${POSTGRES_DB} to ${SOURCE_APP_USER};

    revoke create on schema public from public;
SQL

echo "postgres-source: ${SOURCE_READ_USER} granted SELECT on core and ref, including future tables"
echo "postgres-source: ${SOURCE_APP_USER} granted CREATE on database ${POSTGRES_DB} for the platform schema"
