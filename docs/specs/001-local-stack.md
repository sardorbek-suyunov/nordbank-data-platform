# 001 — Local Stack

Status: Approved
Version: 2
Supersedes: version 1, 2026-09-18, unimplemented
Depends on: 000

## Goal
A reproducible local runtime for the platform: source database, object storage,
warehouse file and Airflow, brought up with one command, verified by an automated health
check and by one DAG that exercises every connection end to end. No business data and no
pipeline logic.

## Scope

### 1. Services
Defined in `docker-compose.yml` at the repository root, with build contexts and support files
under `infra/docker/`. Every long-running service has a healthcheck, a restart policy, an
explicit memory limit and a pinned image; no `latest`.

| Service | Image | Purpose |
|---|---|---|
| `postgres-source` | `postgres:16.15` | Simulated core banking source database |
| `postgres-airflow` | `postgres:16.15` | Airflow metadata database, deliberately separate |
| `minio` | `quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z@sha256:14cea493d9a34af32f524e538b8346cf79f3321eff8e708c1e2960462bd8936e` | S3-compatible lake |
| `minio-init` | `quay.io/minio/mc:RELEASE.2025-08-13T08-35-41Z@sha256:a7fe349ef4bd8521fb8497f55c6042871b2ae640607cf99d9bede5e9bdf11727` | One-shot bucket and prefix provisioning, exits 0 |
| `airflow-init` | project image | One-shot migrate, admin user, pool, warehouse init, exits 0 |
| `airflow-apiserver` | project image | Airflow 3 API server and UI |
| `airflow-scheduler` | project image | Scheduler |
| `airflow-dag-processor` | project image | DAG parsing |
| `airflow-triggerer` | project image | Deferrable task execution |

One-shot services (`minio-init`, `airflow-init`) carry no healthcheck: they exit as soon as
their work is done, so a healthcheck would have nothing to poll. Their contract is exit 0,
consumed by dependants through `depends_on: condition: service_completed_successfully`.

Pinning rule: an image whose registry history has proven unstable is pinned by tag **and**
digest; everything else is pinned by tag. MinIO qualifies, having been withdrawn from Docker
Hub. Executor is `LocalExecutor`. No Celery, no Redis.

### 2. Airflow image
`infra/docker/airflow/Dockerfile`, based on `apache/airflow:3.3.2-python3.11`, installing
runtime dependencies against the official constraints file for that version
(`constraints-3.3.2/constraints-3.11.txt`).

M1 dependency set: `pyarrow`, `boto3`, `psycopg2-binary`, `requests` (all present in the
constraints file), plus two the constraints do not cover and which are therefore pinned
explicitly in `infra/docker/airflow/requirements.txt`:

- `duckdb==1.5.5`. The version is chosen for compatibility with the `dbt-duckdb` release that
  arrives at M4, which requires `duckdb>=1.0.0` and pins `1.5.5` in its MotherDuck extra, not
  because it is the newest.
- `apache-airflow-providers-fab`, required by the auth manager in section 7. The build
  verifies it is importable rather than assuming the base image ships it.

dbt is not installed until M4.

### 3. Source database provisioning
`infra/docker/postgres-source/init/` scripts, applied on first start:
- Database `nordbank`.
- Schemas `core` and `ref`.
- Role `nordbank_app`, owner of both schemas, used by the generator.
- Role `nordbank_reader`, `SELECT`-only on both schemas including default privileges for
  future tables, used by extraction. Extraction never authenticates as an owner.
- `SET timezone = 'UTC'` at database level.
- No business tables. DDL is M2.

### 4. Object storage provisioning
`minio-init` creates bucket `nordbank-lake`, creates the `bronze/` and `quarantine/` prefixes,
and confirms anonymous access is denied. Idempotent: re-running changes nothing.

Bucket versioning is deliberately **not** used. Immutability is a property of the key, not of
a bucket feature (ADR 0008): every object carries the batch id that produced it, so no two
writes can target the same key.

```
bronze/<source>/<entity>/ingest_date=YYYY-MM-DD/batch_id=<batch_id>/part-NNNN.parquet
```

### 5. Warehouse initialisation
A DuckDB file at the path given by `DUCKDB_PATH`, on a named volume shared by the Airflow
services. `airflow-init` creates it if absent and creates the six schemas `bronze`, `silver`,
`gold`, `dq`, `ops`, `meta`. It also creates `ops.stack_health_probe` with columns `probe_id`,
`probed_at`, `component`, `detail`, for use by the health-check DAG, and the Airflow pool
`warehouse_access` with exactly one slot. No other objects.

`warehouse_access` guards every kind of warehouse access, not only writes. A DuckDB write lock
excludes readers as well as writers, so a read task that does not take the pool can fail to
open the file while another task holds it.

### 6. Declarative connection provisioning
Two Airflow connections, supplied as `AIRFLOW_CONN_*` environment variables in
`docker-compose.yml`, sourced from `.env`:

- `nordbank_source_db`, URI format.
- `nordbank_lake`, JSON format, because the S3 endpoint override is unreadable when
  percent-encoded into a URI query string.

Nothing is created through the UI, and no connection state exists outside version-controlled
configuration.

The warehouse is **not** an Airflow connection. There is no DuckDB connection type in core
Airflow, so one would be a label with no hook behind it, implying Airflow manages something it
does not. The path comes from `DUCKDB_PATH`, and all access goes through one helper module in
`airflow/plugins/` that owns the path, the read-only flag, the bounded retry and backoff on
lock conflict, and the pool discipline. The helper is unit-testable without a running stack.

Rationale in `docs/adr/0007-declarative-airflow-configuration.md`.

### 7. Airflow configuration
Set explicitly in compose, not left to defaults: `LOAD_EXAMPLES=False`,
`DEFAULT_TIMEZONE=utc`, `AUTH_MANAGER` set to the FAB auth manager,
`AIRFLOW__API_AUTH__JWT_SECRET` and `AIRFLOW__CORE__FERNET_KEY` from `.env`,
`AIRFLOW__CORE__EXECUTION_API_SERVER_URL` pointing at the API server, DAG directory mounted
read-only from `airflow/dags`, plugins from `airflow/plugins`, logs to a named volume rather
than a host bind, admin user from `.env`, and `parallelism` and `max_active_tasks_per_dag` set
to values a laptop survives.

FAB is used rather than the Airflow 3 default simple auth manager because it is the path the
official compose takes and because it provides the roles that the M8 access-control work will
reference.

`airflow-init` creates pool `warehouse_access` with one slot, described as the DuckDB
single-access guard, idempotently.

### 8. Health-check DAG
`airflow/dags/ops_stack_healthcheck.py`, DAG id `ops_stack_healthcheck`, schedule `None`, no
catchup, tags `ops`. Four tasks:

1. Query `postgres-source` as `nordbank_reader`, assert `core` and `ref` exist.
2. List `nordbank-lake`; write two probe objects that differ only by their `batch_id` key
   segment, assert both exist as distinct keys, then delete both.
3. Acquire pool `warehouse_access`, open DuckDB read-write, insert one row into
   `ops.stack_health_probe`, assert the six schemas exist.
4. Acquire pool `warehouse_access`, open DuckDB read-only, read back the row written by task 3.

Task logic lives in `airflow/plugins/` as importable functions, not inline in the DAG file, so
it is unit-testable. Tasks must be idempotent and safe to re-run.

### 9. Make targets
Replace the M1 stubs with real implementations, delegating to `scripts/`:

- `up` — verify the Docker daemon is reachable and has at least 6 GB allocated, failing with
  an actionable message if not; build if needed; start; block until every healthcheck passes
  or fail with the name of the unhealthy service and its last log lines.
- `down` — stop, keep volumes.
- `nuke` — stop and remove volumes, with a typed confirmation. `FORCE=1` skips the prompt so
  the target is usable from a script.
- `health` — per-service probe table with pass or fail per component, non-zero exit on any
  failure. Probes: `pg_isready` on both databases, `mc ls` on the bucket, DuckDB schema count,
  Airflow API health endpoint. A warehouse held by another process reports `busy` and counts as
  a pass, because a held lock is the system working as designed; `STRICT=1` treats `busy` as a
  failure and is what CI uses.
- `logs` — follow, optional `SERVICE=` filter.
- `test` — unit tests that do not import Airflow. Runs anywhere, including native Windows. No
  tolerance for an empty collection.
- `test-dags` — DAG integrity tests, which require Airflow. Runs natively where Airflow
  imports, and inside the project image via `docker compose run --rm --no-deps` where it does
  not. The target prints which path it took; it never chooses silently.
- `test-integration` — smoke tests against a running stack.

### 10. Tests
`airflow/tests/`:
- `test_dag_integrity.py` — every DAG in `airflow/dags` imports without error, has no cycles,
  has an owner and tags, and its DAG id matches its filename. Passes with zero DAGs and is
  never skipped when empty.
- `test_stack_smoke.py` — marked `integration`, skipped when the stack is not running: both
  databases reachable, bucket present with its prefixes, anonymous access denied, batch-id keys
  distinct, DuckDB has the six schemas, Airflow API healthy.
- `test_healthcheck_functions.py` — unit tests for the plugin functions with connections faked,
  including the warehouse helper retry and read-only behaviour. No running stack, no Airflow
  import.

Register the `integration` marker in `pyproject.toml`. Airflow goes in a separate `airflow`
dependency group so that `uv sync` on a machine where Airflow does not run does not install a
broken Airflow into the default environment.

### 11. Continuous integration
Extend `.github/workflows/ci.yml`:
- `lint` also runs `docker compose config --quiet`, which needs no daemon and catches YAML and
  interpolation errors before the expensive workflow runs, and `make test`.
- A `dags` job installs the `airflow` group on Linux and runs `make test-dags`.

Add `.github/workflows/stack.yml`, triggered on changes to `docker-compose.yml`,
`infra/docker/**` or `airflow/**`, and on manual dispatch: build the image with buildx layer
caching, bring the stack up, run `make health STRICT=1`, trigger `ops_stack_healthcheck`,
assert it succeeds and that its warehouse tasks ran in pool `warehouse_access`, then tear down.
Every wait loop has an explicit timeout. The DAG processor refresh interval is lowered in CI so
a trigger does not wait on a parse cycle. Fail the job on any unhealthy service, and upload
compose logs as an artifact on failure.

### 12. Documentation
- `docs/runbook.md` — fill the local setup and service endpoints sections: URL, port,
  credential source and purpose per service; first-run procedure; teardown; the observed Docker
  resource configuration the stack was validated against; that Airflow does not run natively on
  Windows and that this is expected; the digest-pinning rule; a contingency naming LocalStack S3
  or SeaweedFS if quay.io becomes unavailable, since the lake layer depends on one vendor's
  registry; and a failure table covering port conflicts, insufficient Docker memory, missing
  Fernet key, WSL2 disk exhaustion, a missing bucket after a partial `nuke`, and a DuckDB lock
  conflict.
- `docs/adr/0006-local-executor.md` — LocalExecutor over Celery.
- `docs/adr/0007-declarative-airflow-configuration.md` — connections and pools from environment
  variables and init scripts rather than UI state, including the admin user and pool living in
  the metadata database and how that state stays reproducible.
- `docs/adr/0008-bronze-immutability-by-key-construction.md` — batch id in the key rather than
  bucket versioning.
- `.env.example` — every new variable with a comment: both database blocks, MinIO credentials
  and endpoint, S3 settings for DuckDB `httpfs`, `DUCKDB_PATH`, `LAKE_BUCKET`, `AIRFLOW_UID`,
  Fernet key, JWT secret, admin user, the two `AIRFLOW_CONN_*` values, and a note that
  `AIRFLOW_UID` and Docker memory allocation are the two settings most likely to differ per
  machine.
- `README.md` — quickstart becomes the real command sequence; M1 marked done.
- `docs/checkpoints/M1-summary.md`.

### 13. Windows and WSL2
Document, and where possible enforce: `AIRFLOW_UID` handling, why logs use a named volume
instead of a host bind, the minimum Docker Desktop memory allocation, and that the repository
should live inside the WSL2 filesystem rather than on a mounted Windows drive if I/O is slow.

## Out of scope
Business table DDL, the generator, dbt, contracts, quality framework, lineage services,
Streamlit, Terraform, BigQuery.

## Acceptance criteria
1. `make up` from a clean clone with `.env` copied from `.env.example` produces a fully healthy
   stack with no manual step.
2. `make health` prints a per-component table and exits non-zero if any component fails.
   Demonstrate this by stopping one service. `make health STRICT=1` additionally fails on a
   `busy` warehouse.
3. `ops_stack_healthcheck` succeeds when triggered, and both of its warehouse tasks are shown to
   have run in pool `warehouse_access`.
4. `make down` then `make up` preserves the DuckDB row written by the previous health-check run.
   `make nuke` then `make up` produces a clean stack.
5. Airflow shows zero example DAGs, one project DAG, and pool `warehouse_access` with one slot.
6. No Airflow connection, variable or pool is created through the UI; all are present after a
   `nuke` and rebuild with no manual intervention. Evidenced by `airflow connections get` inside
   a container and by the health-check DAG passing, since environment-defined connections are
   deliberately invisible to the UI and to `airflow connections list`.
7. Extraction credentials cannot write: prove `nordbank_reader` is refused on a `CREATE TABLE`
   in `core`.
8. The lake bucket exists, the `bronze/` and `quarantine/` prefixes are present, anonymous
   access is denied, and two writes sharing an ingest date but carrying different batch ids
   produce two distinct keys rather than an overwrite. Prove it.
9. `make test` passes and does not tolerate an empty collection. `make test-dags` passes and
   reports which path it took. `make test-integration` passes against the running stack.
10. Both CI workflows pass on GitHub, including `stack.yml`.
11. Every image is pinned to an explicit version, the MinIO images by digest as well as tag; no
    `latest` anywhere.
12. Runbook and ADRs 0006, 0007 and 0008 exist; `.env.example` documents every new variable.

## Changelog

### Version 2, 2026-09-18

Reissued rather than amended: the review produced more than five corrections to a
specification that had not been implemented.

1. **Pool renamed from `warehouse_write` to `warehouse_access`, one slot, taken by every task
   that touches the warehouse.** A DuckDB write lock excludes readers, not only other writers
   (measured, see ADR 0002), so serialising writers alone would still let a read task fail to
   open the file. Sections 5, 7 and 8 and criteria 3 and 5 changed with it.
2. **The warehouse is no longer an Airflow connection.** No DuckDB connection type exists in
   core Airflow; the `fs` connection considered in review would have implied Airflow manages
   the warehouse. Two connections remain, plus `DUCKDB_PATH` and a helper module that owns the
   path, read-only flag, retry and pool discipline.
3. **Bucket versioning dropped.** Versioning requires the erasure-coded backend, which means
   four volumes and roughly double the storage on the `full` profile, to buy a guarantee the
   key scheme provides for free. Replaced by the batch id in the object key, recorded in
   ADR 0008. Criterion 8 changed from proving versioning to proving that two writes sharing an
   ingest date produce distinct keys.
4. **MinIO images move to quay.io and are pinned by digest as well as tag.** The Docker Hub
   repositories no longer resolve. The newest verified release is used rather than an older one
   with a web console, because `mc` is the admin path.
5. **Airflow 3 configuration made explicit**: FAB auth manager, `AIRFLOW__API_AUTH__JWT_SECRET`
   and `AIRFLOW__CORE__EXECUTION_API_SERVER_URL`. Without them the admin user, the API server
   and task execution do not behave as version 1 assumed. `apache-airflow-providers-fab` is
   pinned in the image and its presence verified.
6. **One-shot services carry no healthcheck.** Version 1 required one on every service; a
   container that exits immediately has nothing to poll.
7. **Test targets split into `test`, `test-dags` and `test-integration`.** Airflow does not
   import on native Windows, and a single target that silently changes where it executes is a
   debugging trap. Airflow moves to its own dependency group.
8. **`make health` gained `busy` and `STRICT=1`**, so a warehouse held by a running task is not
   reported as a failure locally while CI stays deterministic.
9. **`make nuke` gained `FORCE=1`**, without which nothing scripted can use it.
10. **`make up` verifies the Docker daemon and its memory allocation** and fails with an
    actionable message below 6 GB.
11. **`duckdb` is pinned explicitly**, since it is absent from the Airflow constraints file, and
    the version is chosen for M4 `dbt-duckdb` compatibility.
12. **`docker compose config --quiet` added to the fast CI job**, to catch YAML and
    interpolation errors without a daemon.
13. Memory limits use `mem_limit` rather than `deploy.resources`, which is swarm-scoped.

## Amendments

Appended during implementation. The scope text above is left as issued; the protocol is in
`docs/specs/README.md`.

### 2026-09-18 — Anonymous access is asserted as `private` as well as `none`

Section 4 requires `minio-init` to confirm anonymous access is denied. The pinned `mc` reports a
bucket with no anonymous policy as `private`, not `none`, so the literal check failed on a
correctly configured bucket. The check now fails on any policy mentioning public, download,
upload or write, accepts `none` and `private`, and fails on anything it does not recognise
rather than assuming the best.

### 2026-09-18 — `pytest` is installed in the project image

Section 2 lists the M1 dependency set. `pytest` is added, because section 9 requires
`make test-dags` to run inside the project image on hosts where Airflow does not import, and
the tests cannot run in an image without a test runner.

### 2026-09-18 — Integration tests execute inside the stack

Section 10 describes `test_stack_smoke.py` as skipped when the stack is not running. It runs
inside `airflow-scheduler` rather than on the host, because the warehouse file is on a named
volume and the service names only resolve on the compose network, so a host-side run could not
reach two of the four things it checks. `make test-integration` prints where it runs. The
individual tests still skip when a dependency or a service is unavailable.

### 2026-09-18 — `SKIP_BUILD=1` for a caller that has already built the image

Section 9 says `make up` builds if needed. CI builds the image through buildx with a layer
cache, so compose must not rebuild it immediately afterwards. `SKIP_BUILD=1` omits `--build`,
and `stack.yml` is the only caller that sets it.

### 2026-09-18 — `.env.example` carries a working development Fernet key and JWT secret

Acceptance criterion 1 requires `make up` to work from a plain copy of `.env.example` with no
manual step. Airflow does not start with a placeholder Fernet key, so the template carries a
real development key and JWT secret, labelled as published and protecting nothing, with the
command to regenerate them. Every password in the template remains a placeholder.

### 2026-09-18 — Airflow 3.3.2 API details in the DAG integrity tests

`DagBag` lives at `airflow.dag_processing.dagbag` and no longer takes `include_examples`, and
cycle checking is `dag.check_cycle()` rather than `airflow.utils.dag_cycle_tester`. Example DAGs
are excluded by `AIRFLOW__CORE__LOAD_EXAMPLES` in compose instead of by a DagBag argument.

### 2026-09-18 — DAG integrity runs in its own CI job

Section 11 puts the DAG integrity tests in the `lint` job. They need the `airflow` dependency
group, which is a heavy install, and the point of the fast job is to stay fast. They run in a
separate `dags` job on the same trigger, so nothing is lost but the ordering.

### 2026-09-18 — `verify-dag` target added

Section 9 lists the required targets. `verify-dag` is added: it triggers
`ops_stack_healthcheck` from the CLI, waits for it, and asserts from the `task_instance` records
that every task succeeded and that both warehouse tasks ran in `warehouse_access`. It is the
scriptable path `stack.yml` uses, and it keeps the UI out of the verification loop.

### 2026-09-18 — Helper scripts read `.env` without exporting it

The health and verify scripts need values from `.env`. Reading them into the process
environment made `docker compose` prefer those values over the file, and one parsing mistake
with an inline comment silently gave a container a different password than the server had. The
scripts now read `.env` into a dictionary through `scripts/env_file.py` and never export.

### 2026-09-18 — Environment variable names from M0 are superseded

`.env.example` renames the M0 placeholders to match what the stack actually consumes:
`POSTGRES_*` becomes `POSTGRES_SOURCE_*` and `POSTGRES_AIRFLOW_*`, MinIO gains explicit port
variables, and the Airflow block gains the admin name fields, the JWT secret and the connection
strings. M0 had no services to consume the old names.

### 2026-09-18 — `AIRFLOW_UID` is fixed at 50000

Section 13 requires `AIRFLOW_UID` handling to be documented and enforced where possible. The
handling is that it must not be changed. The usual advice to set it to `id -u` on Linux exists
because the official compose file bind-mounts writable directories; here every writable path is
a named volume initialised from the image and every bind mount is read-only, so there is nothing
to own. Setting it to the runner uid made `airflow-init` fail immediately with
`ModuleNotFoundError: No module named 'airflow'`, because Airflow is installed in user 50000's
home. CI found this, and the value is now left alone in the workflow, the template and the
runbook.
