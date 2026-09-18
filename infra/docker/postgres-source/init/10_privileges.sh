#!/bin/bash
# Privileges for the extraction role. It reads and nothing else, including tables that do not
# exist yet, which is what the default privileges are for. Extraction never authenticates as an
# owner.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER}" --dbname "${POSTGRES_DB}" <<-SQL
    grant connect on database ${POSTGRES_DB} to ${SOURCE_READ_USER};
    grant usage on schema core, ref to ${SOURCE_READ_USER};
    grant select on all tables in schema core, ref to ${SOURCE_READ_USER};

    alter default privileges for role ${SOURCE_APP_USER} in schema core
        grant select on tables to ${SOURCE_READ_USER};
    alter default privileges for role ${SOURCE_APP_USER} in schema ref
        grant select on tables to ${SOURCE_READ_USER};

    revoke create on schema public from public;
SQL

echo "postgres-source: ${SOURCE_READ_USER} granted SELECT on core and ref, including future tables"
