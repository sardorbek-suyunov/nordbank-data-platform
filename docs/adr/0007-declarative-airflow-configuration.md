# 0007 — Declarative Airflow configuration

Status: Accepted
Date: 2026-09-18

## Context

Airflow keeps connections, variables and pools in its metadata database, and its UI invites an
operator to create them by hand. Anything created that way exists in one database, on one
machine, with no history of who changed it or why, and it disappears when the volume is
removed. A stack that needs a documented click-path before it works is not reproducible.

## Decision

Everything Airflow needs to reach another system is supplied by configuration, not by the UI.

Connections come from `AIRFLOW_CONN_*` environment variables declared in `docker-compose.yml`
and sourced from `.env`: `nordbank_source_db` as a URI, `nordbank_lake` as JSON, because the S3
endpoint override is unreadable when percent-encoded into a URI query string.

The warehouse is deliberately not a connection. Core Airflow has no DuckDB connection type, so
one would be a label with no hook behind it, implying Airflow manages a file it does not. The
path comes from `DUCKDB_PATH`, and every access goes through one helper module in
`airflow/plugins/` that owns the path, the read-only flag, the retry on lock conflict and the
pool discipline.

The admin user and the `warehouse_access` pool cannot be environment variables: Airflow reads
them from the metadata database. They are created by `airflow-init` on every start, idempotently,
from values in `.env`. The state lives in the database, but nothing is authored there: the
database is a cache of what configuration already says, and deleting the volume loses nothing
that `make up` does not put back.

## Consequences

Environment-defined connections are invisible in the UI and to `airflow connections list`.
An engineer looking for a connection will not find it where Airflow's own documentation tells
them to look, and the first debugging session after that discovery is wasted. The runbook has
to say this, and `airflow connections get` is the command that answers the question instead.

Secrets sit in the process environment of every Airflow container, readable by anyone who can
exec into one, and visible in `docker inspect`. That is acceptable for a local stack with
synthetic data and would not be acceptable for a real deployment, which is what a secrets
backend is for.

Changing a connection means editing `.env` and restarting the services, rather than editing a
form. That is slower per change and correct in the aggregate, since the change is then in the
file the next person reads.

## Alternatives considered

**Create connections and pools in the UI.** Rejected because the configuration then exists
only inside a volume: it survives no rebuild, appears in no review, and has no history. The
first `make nuke` would silently break the stack in a way that only a human who remembered the
click-path could repair.

**A secrets backend such as Vault or AWS Secrets Manager.** Rejected for this milestone
because it adds a service to run, credentials to bootstrap and a network dependency, to protect
placeholder credentials for a synthetic bank. The connection ids stay stable, so moving to a
backend later changes where values come from and not what reads them.

**A JSON connections file imported by `airflow connections import`.** Rejected because it is a
second source of truth beside `.env`, and because the import is a step that can be skipped,
which is exactly the failure this record exists to prevent.
