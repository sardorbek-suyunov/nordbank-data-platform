# M1 — Local stack

Specification: [001-local-stack.md](../specs/001-local-stack.md), version 2
Status: complete
Date: 2026-09-18

## What was built

A local runtime that comes up with one command and proves itself with one DAG. No business
data, no pipeline logic.

- `docker-compose.yml` with nine services: two Postgres instances, MinIO, a one-shot `mc`
  provisioner, and the Airflow 3 set (init, api-server, scheduler, dag-processor, triggerer)
  on `LocalExecutor`. Every long-running service has a healthcheck, a restart policy, a memory
  limit and a pinned image; the MinIO images are pinned by digest as well as tag.
- `infra/docker/airflow/` builds the project image from `apache/airflow:3.3.2-python3.11`
  against the official constraints file, adding `duckdb==1.5.5`, `pyarrow`, `boto3`,
  `psycopg2-binary`, `requests`, `apache-airflow-providers-fab` and `pytest`, and verifying at
  build time that they import.
- Source database provisioning: `core` and `ref` schemas, an owner role and a `SELECT`-only
  extraction role with default privileges for future tables, UTC at database level.
- Lake provisioning: bucket, `bronze/` and `quarantine/` prefixes, anonymous access denied,
  idempotent.
- Warehouse initialisation: the DuckDB file, its six schemas, `ops.stack_health_probe`, and the
  `warehouse_access` pool with one slot.
- `airflow/plugins/nordbank_ops/`: the warehouse helper that owns the path, the read-only flag,
  the retry on lock conflict and the pool name; lake key construction; source database
  assertions; and the one module that turns Airflow connections into clients.
- `airflow/dags/ops_stack_healthcheck.py`: four tasks covering the source database, the lake
  including the batch-id key property, and the warehouse in both directions.
- Make targets: `up`, `down`, `nuke`, `health`, `logs`, `verify-dag`, `test`, `test-dags`,
  `test-integration`, each delegating to a script in `scripts/`.
- Tests: 13 unit tests that need neither Airflow nor a stack, 6 DAG integrity tests, 8 smoke
  tests against a running stack.
- CI: `ci.yml` gains compose validation, unit tests and a `dags` job; `stack.yml` builds the
  image with a layer cache, brings the stack up, runs the strict health probes, triggers and
  verifies the DAG, runs the smoke tests, and uploads compose logs on failure.

## Measurements

Validated against Docker 29.6.2, 32 CPUs, 31.2 GiB available to the daemon, storage driver
overlayfs.

| Measurement | Value |
|---|---|
| First `make up` (image build and all pulls) | about 6 minutes |
| `make up` from a `make nuke` | 38 to 39 seconds |
| `make up` after `make down` | 33 to 34 seconds |
| Memory limits, long-running services | 7680 MiB total, about 7.5 GiB |
| Measured steady-state usage | about 1.4 GiB total |

The stack is validated at the documented floor rather than at what this machine has: the limits
are in force in `docker-compose.yml`, and the run above was made with them applied.

## Deliberately deferred

| Deferred | To |
|---|---|
| Source DDL, `core` and `ref` tables, initial historical load | M2 |
| Daily mutation engine | M3 |
| dbt project, bronze models, contracts, quarantine tables, identifier tokenisation | M4 |
| Silver conformance, SCD2, generalisation | M5 |
| Gold dimensional model and marts | M6 |
| Data quality gates, reconciliation, the orphan-object reaper, ops observability | M7 |
| Lineage, erasure DAG, PII vault, access control roles on top of FAB | M8 |
| Power BI, Streamlit, the `exports/` snapshot task | M9 |
| BigQuery target, Terraform | M10 |

## Known gaps

- The warehouse file is not reachable from the host, by design. Probes and smoke tests run
  inside a container, which makes them slower and makes a failure harder to reproduce by hand.
- `make health` spawns a short-lived `mc` container for the bucket probe, which costs a second
  or two per run. Reusing a running container would be faster and would probe less honestly.
- The DAG integrity tests run inside the project image on Windows, so they exercise the image
  rather than the developer environment. On Linux and in CI they run natively.
- The health-check DAG writes a row per run into `ops.stack_health_probe` and never prunes it.
  Harmless at this volume; it belongs with the retention work at M7.
- Compose does not enforce a CPU limit, only memory. A runaway task can still take the machine.
- The first `make up` is dominated by the image build, and nothing caches that between clones.
  CI caches it; a developer with a fresh clone pays it once.

## Verifying the repository

From the repository root. On Windows, run these from Git Bash with GNU make on the PATH.

```bash
cp .env.example .env     # a plain copy is enough; no manual step
make up                  # preflight, build if needed, start, wait for health
make health              # per-component table; STRICT=1 treats a busy warehouse as failure
make verify-dag          # trigger ops_stack_healthcheck from the CLI and verify from records
make test                # unit tests, no Airflow, no stack
make test-dags           # DAG integrity, natively or in the project image
make test-integration    # smoke tests inside the running stack
make down                # stop, keep volumes
FORCE=1 make nuke        # stop and delete volumes
```
