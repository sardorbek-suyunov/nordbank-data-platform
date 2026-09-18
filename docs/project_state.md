# Project state

## What this is

Nordbank is a simulated EU-licensed neobank, and this repository is the data platform behind
it: a synthetic core banking system and four external feeds ingested into a medallion lakehouse
on MinIO and DuckDB, orchestrated by Airflow, transformed by dbt, and consumed by a Power BI
semantic model and a Streamlit application. It exists to answer the nineteen numbered questions
in [business_questions.md](business_questions.md), which are the acceptance baseline for the
gold layer.

## Current state

**M0 and M1 are complete. M2 has not started.**

What exists and runs today:

- A local stack of nine services brought up by `make up`: a source Postgres with `core` and
  `ref` schemas and two roles, an Airflow metadata Postgres, MinIO with the `nordbank-lake`
  bucket, and Airflow 3.3.2 on LocalExecutor (init, api-server, scheduler, dag-processor,
  triggerer).
- A DuckDB warehouse file on a named volume with six schemas (`bronze`, `silver`, `gold`, `dq`,
  `ops`, `meta`) and the `ops.stack_health_probe` table.
- One DAG, `ops_stack_healthcheck`, which exercises every connection: the source database as
  the read-only role, the lake including the batch-id key property, and the warehouse in both
  directions through the `warehouse_access` pool.
- `make` targets for the full lifecycle: `init-env`, `up`, `down`, `nuke`, `health`, `logs`,
  `verify-dag`, `test`, `test-dags`, `test-integration`, `lint`, `format`, `clean`.
- 26 unit tests, 6 DAG integrity tests, 8 smoke tests, and two CI workflows.
- The documentation set: architecture, conventions, business questions, metric definitions,
  data dictionary, model inventory, PII classification, runbook, two specifications and eight
  decision records.

What does not exist yet: any business table, any generated data, any ingestion, the dbt
project, any bronze, silver or gold model, the data contracts, the quality framework, the
governance work, Power BI, Streamlit, Terraform and the BigQuery target. The source database is
empty by design; its DDL is M2.

## Environment facts

These are properties of the machine this was built and validated on, not of the repository.

- **Host is Windows**, and the shell is Git Bash. The Makefile sets `SHELL := bash`, so make
  targets are run from Git Bash rather than PowerShell or cmd.
- **GNU make 4.4.1 was installed with winget** (`ezwinports.make`); it is not present on a
  stock Windows machine.
- **Docker Desktop**, validated against Docker 29.6.2 with 32 CPUs, 31.2 GiB available to the
  daemon and the overlayfs storage driver. `make up` refuses to start below a 6 GiB floor. The
  per-service limits total 5632 MiB, and the stack was validated with those limits in force
  rather than on an unconstrained machine.
- **Airflow does not import natively on this platform.** `import airflow` fails on Windows;
  Airflow supports POSIX hosts. This is expected rather than a defect: `make test` never needs
  Airflow, and `make test-dags` runs the DAG integrity tests inside the project image and says
  which path it took. On Linux and in CI they run natively.
- **`.env` is generated, not copied.** `.env.example` documents variables and contains no
  working secret: every credential is the sentinel `__GENERATE__` or `__EXTERNAL__`.
  `make init-env` fills them, `make up` does it automatically when `.env` is missing, and both
  validate the result before anything starts.
- **`main` is protected.** Four required status checks (`lint`, `dags`, `docs`, `stack`), a
  pull request required, `enforce_admins` on, force pushes and deletions refused. Direct pushes
  to `main` are rejected for everyone including the repository owner.
- **Rebase merge only.** Squash merging and merge commits are disabled, because the commits in
  a branch are written to be individually reviewable.

## Repository map

| Path | Contents | Authority |
|---|---|---|
| `docs/` | Every document in the project: specifications, decision records, conventions, checkpoints | Authoritative for intent, design and decisions |
| `airflow/` | DAGs, plugins, orchestration and unit tests | Authoritative for orchestration and for the shared operational code in `plugins/nordbank_ops/` |
| `dbt/` | Model tree, snapshots, macros, tests, seeds | Will be authoritative for transformation from M4; currently structure only |
| `generator/` | Synthetic core banking source system and its mutation engine | Will be authoritative for source data from M2; currently empty |
| `infra/` | `docker/` build contexts and init scripts, `terraform/` for the cloud sandbox | Authoritative for how services are built and provisioned |
| `contracts/` | Per-source data contracts and drift behaviour | Will be authoritative for what bronze accepts, from M4 |
| `quality/` | Check definitions, severities, freshness SLAs | Will be authoritative for data quality from M7 |
| `analytics/` | Streamlit application | Will be authoritative for the public deployment from M9 |
| `scripts/` | Scripts invoked by the Makefile, by CI and by hand | Authoritative for operational tooling; any make recipe over three lines lives here |
| `.github/` | CI workflows | Authoritative for what must pass before a merge |

At the root: `docker-compose.yml` defines the stack, `Makefile` is the entry point for every
task, `pyproject.toml` holds the Python project and its dependency groups, and `.env.example`
documents the environment.

## Locked decisions index

These govern work rather than describe it. Read the relevant one before changing anything in
its area.

| Document | Governs |
|---|---|
| [conventions.md](conventions.md) | Naming, numeric types, currency provenance, dimensional modelling, tests, commits, contribution workflow |
| [architecture.md](architecture.md) | Sources and ingestion patterns, layer contracts, storage layout, orchestration, consumption, security, scale profiles |
| [pii_classification.md](pii_classification.md) | The four column classes and the handling rule for each |
| [metric_definitions.md](metric_definitions.md) | The exact rule behind every term a model could compute two defensible ways |
| [model_inventory.md](model_inventory.md) | Every planned model with its layer, grain, inputs, the questions it serves and the milestone that builds it |
| [business_questions.md](business_questions.md) | The nineteen questions gold is measured against, and the coverage rule in both directions |
| [data_dictionary.md](data_dictionary.md) | The nineteen source entities with domain, grain and load pattern |

## Specifications and decision records

| Specification | Status | Summary |
|---|---|---|
| [000](specs/000-repository-foundation.md) | Approved, implemented | Repository skeleton, tooling, conventions, architecture documentation and the requirements baseline |
| [001](specs/001-local-stack.md) | Approved version 2, implemented | The local runtime: services, images, provisioning, the health-check DAG, make targets, tests and CI |

Specification 002, which governs M2, has not been written.

| Record | Status | Decision |
|---|---|---|
| [0001](adr/0001-medallion-architecture.md) | Accepted | Bronze, silver and gold as separate layers with separate guarantees |
| [0002](adr/0002-duckdb-as-warehouse.md) | Accepted, corrected 2026-09-18 | DuckDB as the primary warehouse; one process holds the file, so all access serialises through one pool |
| [0003](adr/0003-synthetic-source-system.md) | Accepted | A generated core banking system rather than a static public dataset |
| [0004](adr/0004-bi-tooling.md) | Accepted | Power BI for the semantic model, Streamlit for the public deployment, Metabase rejected |
| [0005](adr/0005-pii-crypto-shredding.md) | Accepted, revised 2026-09-18 | Tokenise at ingest, vault the mapping, erase by deleting the vault entry |
| [0006](adr/0006-local-executor.md) | Accepted | LocalExecutor over Celery for a single-machine deployment |
| [0007](adr/0007-declarative-airflow-configuration.md) | Accepted | Connections and pools from configuration, never from the UI |
| [0008](adr/0008-bronze-immutability-by-key-construction.md) | Accepted | The batch id in the object key rather than bucket versioning |

Milestone checkpoints are in [checkpoints/](checkpoints/), one per completed milestone.

## Verification

From a clean clone, with Docker Desktop running and at least 6 GiB allocated. No `.env` step:
`make up` generates and validates one.

```bash
make install             # virtual environment and git hooks
make up                  # generate .env, validate, build, start, wait for health
make health              # per-component table; STRICT=1 also fails on a busy warehouse
make verify-dag          # trigger ops_stack_healthcheck from the CLI, verify from task records
make test                # unit tests, no Airflow and no stack needed
make test-dags           # DAG integrity, natively or inside the project image
make test-integration    # smoke tests inside the running stack
```

Expected timings and results:

| Step | Expectation |
|---|---|
| First `make up` on a new clone | About 6 minutes, dominated by the Airflow image build |
| `make up` after `make nuke` | 36 to 39 seconds |
| `make up` after `make down` | 33 to 34 seconds, and the warehouse contents survive |
| `make health` | Five components, all `pass`, exit 0 |
| `make verify-dag` | Four tasks succeed; both warehouse tasks report pool `warehouse_access` |
| `make test` | 26 passed |
| `make test-dags` | 6 passed, with the execution path printed |
| `make test-integration` | 8 passed |

Steady-state memory after a successful run is about 1.3 GiB against 5.5 GiB of limits.

## Known gaps and open decisions

Deferred work, with the milestone that owns it:

| Deferred | Owner |
|---|---|
| Source DDL, `core` and `ref` tables, initial historical load | M2 |
| Daily mutation engine: updates, soft deletes, late arrivals, schema drift | M3 |
| dbt project, bronze models, contracts, quarantine, identifier tokenisation | M4 |
| Silver conformance, SCD2, quasi-identifier generalisation | M5 |
| Gold dimensional model and the marts | M6 |
| Quality gates, reconciliation, orphan-object reaper, retention, ops observability | M7 |
| Lineage, erasure DAG, PII vault, access control roles | M8 |
| Power BI, Streamlit, the `exports/` snapshot task | M9 |
| BigQuery target, Terraform | M10 |

Open decisions, both of which block only the marts that consume them and must be resolved
before M6:

- **Interchange rates** beyond the regulated intra-EEA consumer caps. Commercial and
  inter-regional rates are negotiated and have no single public number; the decision depends on
  which card products the generator issues at M2.
- **The funding cost assumption** behind the net interest income proxy. It requires deciding a
  funding mix that the source system does not model yet. Until then question 5 reports gross
  interest accrued and says that funding cost is excluded.

Other known gaps:

- **The tightest resource margin is the dag-processor and the triggerer**, both at roughly
  1.7x headroom (512 MiB limit against about 300 MiB measured). They are the first pair to
  reconsider when a milestone adds runtime work.
- The warehouse file is not reachable from the host by design, so probes and smoke tests run
  inside a container.
- `ops.stack_health_probe` grows by one row per health-check run and nothing prunes it.
- Compose limits memory but not CPU.
- Two silver models named in the model inventory, `sl_card_settlements` and
  `sl_macro_indicators`, read from sources whose entities are not in the M0 entity inventory.
- The architecture diagram is a link to a directory rather than a diagram.

## Next milestone

**M2, the source system.** It creates the `core` and `ref` DDL, the column-level data
dictionary deferred from specification 000, the PII classification on every column, and the
initial historical load, writing through `COPY` rather than row by row so that the `full`
profile stays usable.

It is governed by specification 002, which is not yet written. Its inputs are the entity
inventory in [data_dictionary.md](data_dictionary.md), the conventions, and
[pii_classification.md](pii_classification.md). Work happens on a `feat/M2-<slug>` branch and
lands through a pull request with all four checks passing.
