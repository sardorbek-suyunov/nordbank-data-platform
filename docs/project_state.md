# Project state

## What this is

Nordbank is a simulated EU-licensed neobank, and this repository is the data platform behind
it: a synthetic core banking system and four external feeds ingested into a medallion lakehouse
on MinIO and DuckDB, orchestrated by Airflow, transformed by dbt, and consumed by a Power BI
semantic model and a Streamlit application. It exists to answer the nineteen numbered questions
in [business_questions.md](business_questions.md), which are the acceptance baseline for the
gold layer.

## Current state

**M0 to M4 are complete.** M4, ingestion into bronze, was three specifications: 005 (extraction
and bronze landing) and 006 (the external feeds), merged, and 007 (the dbt project and bronze
models), delivered on `feat/M4-dbt-bronze`. The M4 checkpoint is
[checkpoints/M4-summary.md](checkpoints/M4-summary.md).

What exists and runs today:

- A local stack of nine services brought up by `make up`: a source Postgres with `core`,
  `ref` and `platform` schemas and two roles, an Airflow metadata Postgres, MinIO with the
  `nordbank-lake` and `nordbank-inbound` buckets, and Airflow 3.3.2 on LocalExecutor (init,
  api-server, scheduler, dag-processor, triggerer).
- The complete source schema, applied by `make schema-apply`: 16 `core` tables, 29 `ref`
  tables and `platform.column_classifications`, with foreign keys all indexed, an
  `updated_at` index and trigger on every table, and a deferred constraint trigger that makes
  an unbalanced ledger batch impossible to commit. Reference data is seeded idempotently.
- A deterministic generator in `generator/`, loaded by `COPY`, and a mutation engine that
  advances the source one business day per transaction through seven change classes, with the
  tick log as a reconciliation control and fourteen coherence invariants that are each proven
  capable of failing.
- The simulated third parties, on the far side of the boundary M3 established: a card processor
  that sends clearing files built from the ledger, with malformed records, late files, breaks,
  corrections, a cut-off transmission and scripted layout drift (`generator/settlement/`), and a
  sanctions publisher that exports a synthetic list in the real feed's shape
  (`generator/sanctions/`, ADR 0015). Both deliver to the inbound bucket.
- A DuckDB warehouse with six schemas. `ops` holds the batch registry, watermarks, the source
  reconciliation, task failures, file identity and sightings and the feed request log; `meta`
  the contract versions, the PII vault and the drift log; `dq` the quarantine index.
- Data contracts under `contracts/`: 45 bootstrapped from the data dictionary for core banking,
  and authored ones for the four feeds, each naming its source of truth. The contract in force
  for a delivery is chosen by the day its sender produced it (ADR 0016), with superseded
  versions kept under `history/`.
- A dbt project with one target, run as a subprocess from its own environment in the image, and
  fifty bronze models generated from the contracts: each a view over its lake prefix that reads
  by column name, casts to the contract and keeps only rows whose object key's batch is
  registered (ADR 0018), with grain, reconciliation, token, object-key and audit tests that
  `make dbt-prove` shows failing on planted fixtures. `transform_bronze` builds and tests bronze
  after every registration; a backfill builds it once at the end. The documentation site is
  published to GitHub Pages from `main` after a scan for every secret and vault value.
- Nine DAGs. `ops_stack_healthcheck` exercises every connection; `ops_source_tick` advances
  the simulated source to its run's logical date, unscheduled and paused by default, with the
  profile the source recorded when it was seeded. `ingest_reference_data` and
  `ingest_core_banking` extract by watermark; `ingest_card_settlements` waits on file arrival
  with a deferrable sensor; `ingest_fx_rates` and `ingest_macro_series` request intervals;
  `ingest_sanctions_list` lands snapshots. Every ingestion DAG is unscheduled and driven by the
  backfill loop, which ticks, delivers and ingests one day at a time.
- Identifiers tokenised at extraction in every mode, the payload of every file and API record
  kept in `_raw_payload` with its identifiers tokenised, quarantine per record with its reason,
  and a PII scan that searches every bronze and quarantine object, bytes and decoded values,
  for every vault value.
- 569 unit tests, 41 DAG integrity tests and 84 integration tests; the unit
  and DAG suites also run with networking disabled (`make test-offline`); two CI workflows, the
  `stack` job seeding at the acceptance anchor, ingesting a week through the real DAGs with no
  live external call and building bronze.
- The documentation set: architecture, conventions, business questions, metric definitions,
  data dictionary, model inventory, PII classification, runbook, eight specifications and
  eighteen decision records.

What does not exist yet: silver, gold, the quality framework and its gates,
the reconciliation mart, freshness monitoring, retention and compaction, the governance work
(lineage, the erasure DAG, access roles), Power BI, Streamlit, Terraform and the BigQuery
target.

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
| `dbt/` | The dbt project: the generated bronze models, the read macro and the generic tests | Authoritative for transformation; the bronze models are generated from `contracts/` and never edited by hand |
| `generator/` | Synthetic core banking source system and its mutation engine | Authoritative for source data and for how it changes |
| `infra/` | `docker/` build contexts and init scripts, `terraform/` for the cloud sandbox | Authoritative for how services are built and provisioned |
| `contracts/` | Per-source data contracts and drift behaviour | Authoritative for what bronze accepts and carries: its columns, types, keys and classifications |
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
| [pii_classification.md](pii_classification.md) | The five column classes and the handling rule for each |
| [metric_definitions.md](metric_definitions.md) | The exact rule behind every term a model could compute two defensible ways |
| [model_inventory.md](model_inventory.md) | Every planned model with its layer, grain, inputs, the questions it serves and the milestone that builds it |
| [business_questions.md](business_questions.md) | The nineteen questions gold is measured against, and the coverage rule in both directions |
| [data_dictionary.md](data_dictionary.md) | Every column of every source table: type, nullability, classification, description and consumer. Generated from it: `platform.column_classifications` and the `make schema-check` comparison |
| [traceability.md](traceability.md) | Every column named in the metric definitions and the model inventory, mapped to the source column that supplies it or the layer that derives it |
| [generator_realism.md](generator_realism.md) | Every distribution the generator draws from, its parameters, the justification, the explicit list of what is deliberately unrealistic, and which behaviours each profile is long enough or large enough to reach. The contract bands it states are checked against `generator/profiles.yml` |

## Specifications and decision records

| Specification | Status | Summary |
|---|---|---|
| [000](specs/000-repository-foundation.md) | Approved, implemented | Repository skeleton, tooling, conventions, architecture documentation and the requirements baseline |
| [001](specs/001-local-stack.md) | Approved version 2, implemented | The local runtime: services, images, provisioning, the health-check DAG, make targets, tests and CI |
| [002](specs/002-source-system-schema.md) | Approved version 2, implemented, amended | The source schema: `core`, `ref` and `platform`, reference seeds, the column-level dictionary and classification, the ERD and the drift control |
| [003](specs/003-historical-load.md) | Approved, implemented, amended | The deterministic generator, the `COPY` loader, the fourteen coherence invariants, the run manifest and the realism model |
| [004](specs/004-mutation-engine.md) | Approved, implemented, amended | The mutation engine: the simulation clock, the tick state machine, seven change classes, the tick log as a reconciliation control, and scripted schema drift |
| [005](specs/005-extraction-and-bronze-landing.md) | Approved, implemented, amended | Extraction and bronze landing: watermark extraction, the batch registry, contracts bootstrapped from the dictionary, quarantine, tokenisation at extraction, schema drift and the interleaved backfill |
| [006](specs/006-external-feeds.md) | Approved version 2, implemented, amended | The four external feeds: snapshot, file-arrival and interval modes, file identity by checksum, contract selection, payload fidelity, the simulated processor and publisher, and recorded fixtures |
| [007](specs/007-dbt-bronze-models.md) | Approved version 2, implemented, amended | The dbt project and bronze models: registry-filtered views over the lake, generated from the contracts, a `ci` ingestion and dbt build in CI, and the documentation on GitHub Pages |

| Record | Status | Decision |
|---|---|---|
| [0001](adr/0001-medallion-architecture.md) | Accepted | Bronze, silver and gold as separate layers with separate guarantees |
| [0002](adr/0002-duckdb-as-warehouse.md) | Accepted, corrected 2026-09-18 | DuckDB as the primary warehouse; one process holds the file, so all access serialises through one pool |
| [0003](adr/0003-synthetic-source-system.md) | Accepted | A generated core banking system rather than a static public dataset |
| [0004](adr/0004-bi-tooling.md) | Accepted | Power BI for the semantic model, Streamlit for the public deployment, Metabase rejected |
| [0005](adr/0005-pii-crypto-shredding.md) | Accepted, revised 2026-09-18, corrected 2026-09-29 | Tokenise at ingest, vault the mapping, erase by deleting the vault entry |
| [0006](adr/0006-local-executor.md) | Accepted | LocalExecutor over Celery for a single-machine deployment |
| [0007](adr/0007-declarative-airflow-configuration.md) | Accepted | Connections and pools from configuration, never from the UI |
| [0008](adr/0008-bronze-immutability-by-key-construction.md) | Accepted | The batch id in the object key rather than bucket versioning |
| [0009](adr/0009-numbered-sql-migrations.md) | Accepted | Numbered idempotent SQL over Alembic or Flyway, with the measured cost of the deferred balance trigger and the partitioning decision |
| [0010](adr/0010-sanctions-screening-through-the-vault.md) | Accepted | Sanctions screening resolves tokens through the vault rather than screening at ingest |
| [0011](adr/0011-copy-loader-with-explicit-identity-keys.md) | Accepted | `COPY` supplies the primary keys into columns that stay `generated always`; foreign keys come off for the load; the ledger analyses before it commits |
| [0012](adr/0012-driver-transport-for-the-mutation-engine.md) | Accepted, corrected 2026-09-21, consequence added | psycopg for the mutation engine, psql for everything that applies a mounted file |
| [0013](adr/0013-file-identity-by-content-checksum.md) | Accepted | A delivered file or snapshot is identified by the SHA-256 of its bytes; deliveries arrive in a separate inbound bucket |
| [0014](adr/0014-contract-of-the-time.md) | Superseded by 0016 for selection | Authored `in_force_from`, superseded versions kept under `history/`, both times kept in `meta.contract_version` |
| [0015](adr/0015-synthetic-sanctions-list.md) | Accepted, consequence added | The sanctions list is the real feed's shape with synthetic content |
| [0016](adr/0016-contract-of-the-delivery.md) | Accepted | The contract in force is chosen by when the sender produced the delivery; a delivery refused with a verdict is parked |
| [0017](adr/0017-mirrored-minio-server.md) | Accepted | The MinIO server from an unmodified mirror pinned by the upstream digest; bucket provisioning with boto3, and no `mc` image |
| [0018](adr/0018-bronze-as-registry-filtered-views.md) | Accepted | Bronze is a generated view per entity over the lake, read by column name and cast to the contract, keeping only rows whose object key's batch is registered |

Milestone checkpoints are in [checkpoints/](checkpoints/), one per completed milestone.

## Verification

From a clean clone, with Docker Desktop running and at least 6 GiB allocated. No `.env` step:
`make up` generates and validates one.

```bash
make install             # virtual environment and git hooks
make up                  # generate .env, validate, build, start, wait for health
make health              # per-component table; STRICT=1 also fails on a busy warehouse
make verify-dag          # trigger ops_stack_healthcheck from the CLI, verify from task records
make schema-apply        # source DDL, reference seeds and the column classifications
make schema-check        # fail if the live schema and the data dictionary disagree
make seed                # generate and load the synthetic operating history
make seed-verify         # the fourteen coherence invariants
make seed-manifest       # write the run manifest; CHECK=1 compares the committed one
make tick                # advance the simulated source one business day
make tick-to DATE=X      # advance to a date, one transaction per day
make tick-status         # the simulation state, the last ten ticks, any drift that has fired
make tick-acceptance     # sixty ticks and specification 004's evidence
make test                # unit tests, no Airflow and no stack needed
make test-dags           # DAG integrity, natively or inside the project image
make test-integration    # smoke tests and the drift check inside the running stack
make test-offline        # unit and DAG suites with no network at all
make warehouse-apply     # the warehouse's ops, meta and dq schema
make contracts-diff      # contract divergence from the dictionary; CHECK=1 fails on it
make backfill FROM=X TO=Y  # tick, deliver and ingest one day at a time, resumable
make ingest-integrity    # the registry against the lake, the watermarks and the quarantine index
make feeds-acceptance    # specification 006's evidence; RUN=name dumps its tables
make bronze-pii-scan     # every bronze and quarantine object, bytes and decoded; PLANT=1 proves it
make fault-demo          # retry and no partial registration against injected faults
make parking-demo        # a refused delivery parked, then released by a contract bump
make feeds-probe         # the live APIs against the recorded fixtures (network)
make dbt-generate        # the bronze models and the inventory's section; CHECK=1 compares
make dbt-prove           # every bronze test and rule failing on planted fixtures, no stack
make dbt-build           # dbt build --warn-error over bronze, inside the stack
make dbt-docs            # the static documentation site, to data/dbt-docs/
make docs-scan           # the site against every secret and vault value; PLANT=1 proves it
make bronze-plant ACTION=plant|verify|remove  # objects the registry never registered
make requirements        # the image's requirement files from the lock; CHECK=1 compares
```

Expected timings and results:

| Step | Expectation |
|---|---|
| First `make up` on a new clone | About 6 minutes, dominated by the Airflow image build |
| `make up` after `make nuke` | 36 to 39 seconds |
| `make up` after `make down` | 33 to 34 seconds, and the warehouse contents survive |
| `make health` | Five components, all `pass`, exit 0 |
| `make verify-dag` | Four tasks succeed; both warehouse tasks report pool `warehouse_access` |
| `make schema-apply` | 14 schema files, 5 seed files, 463 classifications; running it twice changes nothing |
| `make test` | 569 passed, 2 skipped (they run in the image) |
| `make test-dags` | 41 passed, with the execution path printed |
| `make seed` | 166,381 rows at `ci` in 11 s |
| `make seed-verify` | 14 invariants, all pass, before and after any number of ticks; invariant 9's statistical band is not asserted at `ci` and says so |
| `make tick` | One business day in about 450 ms at `ci`; refuses an out-of-order or skipped date by naming the one it expected |
| `make tick-acceptance` | Sixty ticks and the evidence for specification 004; `REPLAY=1` proves replay determinism |
| `make test-integration` | 84 tests, `schema-check` reports 463 columns agreeing, then `seed-verify`; refuses a loaded warehouse unless `FORCE=1`, because it reseeds the source; runs in CI's `stack` job |
| `make test-offline` | 612 passed with `--network none`, about 70 seconds; httpfs loads from the build-time install |
| `make backfill FROM=2026-07-20 TO=2026-09-18` | About an hour at `ci` after a fresh seed at the acceptance anchor, 59 seconds a day, then one bronze build of the whole history in about 30 seconds. Specification 007's runs ended with 2,971 batches, 4 explicitly failed (the scripted cut-off transmission and its reattempt), none open. The fourth acceptance run, before the registry fix below, had 3,000 |
| `make ingest-integrity` | `clean`, seven checks, across every batch: every identifier token resolves in the vault, and no entity has more physical schemas than contract versions |
| `make dbt-build` | 50 views and 267 tests, 318 passing with `--warn-error` and no warning, in 10 to 20 seconds at `ci` |
| `make dbt-prove` | About 18 seconds; every test and rule fails on its planted fixture and passes elsewhere |
| `make docs-scan` | No secret and no vault value; the site shows 811 of 811 classifications. `PLANT=1` catches a planted secret and vault value |
| `make feeds-acceptance RUN=name` | One section per criterion and for the review's rulings; the dump searched for vault values finds none |
| `make bronze-pii-scan` | About 40 seconds; 3,023 objects, no cleartext identifier in either reading; ten fixture names reported as list content. `PLANT=1` catches a card reference in a clearing file, a list name in a core banking object and a card reference in a snapshot |
| `make asset-events-check` | Every run's asset events are exactly its registered batches: 2,996 against 2,996 in the fourth run |
| `make fault-demo` | Six scenarios: each fault recovers and registers, or exhausts and registers nothing |
| `make parking-demo` | Five expectations, all `holds` |

Measured after the third acceptance backfill, on a restarted Airflow with every DAG unpaused
and a settlement sensor deferred: dag-processor 183 MiB and triggerer 288 MiB, each against a
512 MiB limit, so 2.8x and 1.8x headroom; scheduler 314 MiB of 2 GiB; MinIO between 256 and
421 MiB of 512 MiB depending on how much of the lake it had just read.

## Known gaps and open decisions

Deferred work, with the milestone that owns it:

| Deferred | Owner |
|---|---|
| Silver conformance, SCD2, quasi-identifier generalisation | M5 |
| Gold dimensional model and the marts | M6 |
| Quality gates, reconciliation, orphan-object reaper, retention, ops observability | M7 |
| Lineage, erasure DAG, PII vault, access control roles | M8 |
| Power BI, Streamlit, the `exports/` snapshot task | M9 |
| BigQuery target, Terraform | M10 |
| Replace the frozen MinIO mirror with a maintained S3-compatible server (ADR 0017) | M10 |

Found at specification 007 and not fixed there:

- **Re-invoking `make backfill` over a range with a parked delivery fails.** The day's settlement
  run is `failed`, correctly, so the loop re-runs the day and the tick guard refuses. A day should
  count as complete when every failed feed batch is a parked one. Specification 007's amendments
  record it.
- **A bronze view built empty is stale until the next build.** FRED's is, until a keyed run
  registers (ADR 0018).
- **Every bronze read lists its prefix and reads every footer.** 2.46 seconds over 2,968 objects at
  `ci`; compaction, M7's, is what bounds it at `full`.
- **The `stack` job takes about fifteen minutes of its thirty**, 14 minutes 32 seconds measured.
  The loop drives Airflow through its REST API and builds bronze once, which took it from 28
  minutes 38 seconds; the window is fixed by coverage and is not shortened.

Resolved at specification 006's final review:

- **A re-run of a registered relational interval landed it again.** The registry gave a cleared
  or re-triggered run of a day that had registered a second batch over the same window. The
  fourth acceptance run showed it on a resume, stopped between 2026-08-03's reference and core
  banking runs: 29 duplicate reference batches, 441 rows. The open step now skips an entity whose
  interval already has a registered batch; measured on a throwaway stack, clearing a successful
  reference run added 29 batches before the change and none after, and so did a resumed day. A
  deliberate re-extraction would need a force flag, which does not exist. The first acceptance
  run's unattributed 29 extra `corebank` batches against the second are probably the same cause,
  and cannot be confirmed, because that run's warehouse is gone.

## Follow-ups outstanding from M2 and M3

None of them blocks M4.

**1. The `full` profile has not been run.** Its customer count was reset from 250,000 to 30,000
by measurement: 250,000 at the per-customer intensity the `dev` profile validates projects to
roughly 190 million transactions, which is hundreds of millions of rows rather than the tens of
millions the specification targets. What moved is the customer count, not the intensity —
intensity is a validated realism parameter, and lowering it would make `full` a less realistic
bank than `dev`, which is the opposite of what a larger profile is for.

At 30,000 customers over five years the projection is about 23 million transactions, 115 million
rows and 28 GB of source database, arithmetic on the `dev` measurement rather than a
measurement. **Measuring it is re-gated on M6.** Run now it measures a seed and occupies the
source database for about forty minutes of wall clock; run once the gold layer exists it
measures seed through gold at twenty-three million transactions and yields the incremental
against full refresh comparison the profile exists to produce. Spec 003 carries the change as
an amendment and `architecture.md` marks every `full` figure as projected.

**2. Q7 and Q8 need a vintage-conditioned default rate at M6.** An overall default rate is not a
stable measure: its denominator's seasoning changes every month, so a growing book shows a
falling default rate with no change in underwriting at all. `metric_definitions.md` now specifies
the measure the marts implement — the twelve-month default rate over vintages originated at
least twelve months before the reporting date — and keeps the overall rate as a loose sanity
check on the generated book rather than as a published figure. `mart_credit_delinquency` and
`mart_credit_underwriting` declare it when they are built.

**3. The 2019 inter-regional caps do not apply to Nordbank, and the card-present key is dropped.**
The caps in European Commission press release IP/19/2311, made binding in cases AT.40049 and
AT.39398, govern the **inbound** corridor: consumer cards issued outside the EEA and used inside
it. They were binding for five years and six months and expired in October 2024. Nordbank is
an EEA issuer, so its non-EEA rows are the outbound corridor, which those caps never covered and
for which no figure is published: they stay null, and Q4 fails loudly on them at M6 rather than
pricing them. The plan to add a card-present dimension to the key of `ref.interchange_rates` is
dropped with them, because it existed only to carry the caps' card-present split;
`core.transactions.is_card_present` stays, populated by the tick, for the questions that read
it.

Measured at specification 006's review, `ref.interchange_rates` holds 210 rows, 21 of them
non-null: intra-EEA consumer debit at 0.2 per cent, credit at 0.3 per cent and prepaid at 0.2
per cent, seven MCC bands each, which is Regulation (EU) 2015/751 and current law. The 189 null
rows are every commercial class in every region, and every consumer class outside the EEA.

## Open decisions

These block only the marts that consume them:

- **Commercial card interchange** remains undecided and should stay that way. Commercial rates
  are negotiated bilaterally and have no published figure, so any number the platform invented
  would make Q4 look precise while being arbitrary. The seed leaves them null, and a null is an
  error at M6 rather than a zero.
- **The funding cost assumption** behind the net interest income proxy. It requires deciding a
  funding mix that the source system does not model yet. Until then question 5 reports gross
  interest accrued and says that funding cost is excluded. Resolve before M6.

Other known gaps:

- **The tightest resource margin is the triggerer**, at 1.8x headroom with a deferred sensor
  live (288 MiB of 512 MiB), then the dag-processor at 2.8x (183 MiB). MinIO reached 421 MiB of
  512 MiB right after serving the acceptance evidence and fell back to 256 MiB under load, so its
  figure is mostly cache, but it is the service nearest its limit at a moment of reading.
- **`ops.file_sighting` grows by the whole inbound inventory on every run**, like
  `ops.stack_health_probe`: discovery sights every file in the inbound prefix each day, and a
  parked file adds a sighting a day for as long as it waits. The fourth acceptance run recorded
  2,368 clearing-file sightings of 73 objects. Retention belongs to M7.
- **FRED has never been called with a key.** `FRED_API_KEY` is not set, so the macro series
  DAG skips with its reason, no keyed response is recorded, and its fixture's shape is verified
  against the publisher's documented example only. The first live run is the check; its asset
  appears in Airflow when its first batch registers.
- **The sanctions contract's shape is compared with the published FollowTheMoney schema by no
  check.** ADR 0015 adopts a metadata-only comparison as the mitigation; it belongs with
  `make feeds-probe` and is not built.
- **A file ingested the day after it was sent, across a contract bump, is read one version too
  new** (ADR 0016). The backfill never produces it.
- The warehouse file is not reachable from the host by design, so probes and smoke tests run
  inside a container.
- `ops.stack_health_probe` grows by one row per health-check run and nothing prunes it.
- Compose limits memory but not CPU.
- Two silver models named in the model inventory, `sl_card_settlements` and
  `sl_macro_indicators`, read from sources whose entities are not in the M0 entity inventory.
  Their bronze columns are now defined by the `cardnet` and `fred` contracts; the silver models
  are M5's.
- The architecture diagram is a link to a directory rather than a diagram. The source schema
  now has one, in [diagrams/erd.md](diagrams/erd.md); the platform-level diagram does not.

## Next milestone

**M5, silver conformance**: SCD2, deduplication on the business key and `updated_at`, the FX
gap fill, and quasi-identifier generalisation, built on the bronze views. What it inherits from
M4 is in the checkpoint's last section: bronze is a view and a read costs a listing, bronze is not
deduplicated, a retired column is null by delivery date, and FRED has never landed.

The notes below were written when M4 began, and are kept as the record of what it had to
resolve; each says how it was.

**M4's backfill must interleave tick and extract. It cannot run sixty ticks and then extract.**
This is a sequencing requirement on the milestone, not a note about a control.

The tick log reconciles exactly against `updated_at` windows only at the tick. A later tick
re-stamps a row an earlier one wrote, and the earlier window loses it permanently — measured on
the historical book, `accounts` retains 6 of the 210 rows that moved in a six-day-old window,
and a single day of movements touches 466 of 736 open accounts. So an extraction of day D run
after day D+1 has ticked reads a window that no longer holds what the log says changed on day D,
and the assertion that bronze received exactly what the source changed cannot be made at all.

The loop is therefore: tick day D, extract day D, register the batch, tick day D+1. `make tick`
and `make tick-to` are single transactions per day precisely so that loop is available, and
`ops_source_tick` exists so Airflow can drive it. `generator/mutation/reconcile.py` carries the
measurement and the reasoning; spec 004 records it as an amendment.

**Business date, posting date and audit time are three different dates on a late-arriving item.**
An offline card transaction presented days late carries `booked_at` on the day of the tap,
`updated_at` on the day it reached the bank, and a ledger entry posted to the period that was
open when it arrived. `architecture.md` and `metric_definitions.md` say so where silver and Q15
meet it. The ledger side never moves; whether the settlement feed presents an item on its
business date or its clearing date is M4's decision.

**If a later milestone wants CI to exercise ingestion rather than only ticking, the CI anchor
has to move and the manifest regenerates.** Right now the `stack` workflow seeds `ci` at the
pinned anchor 2026-09-18 and runs `make tick-to`; it never runs an ingestion DAG, so nothing
in CI depends on the simulated date being in the past and the pinned anchor is fine. That
changes the moment a workflow runs `ingest_core_banking`: Airflow will not schedule a run
whose logical date is in the future, and a run's logical date is the simulated business day,
so the anchor would have to sit behind the real clock by at least the window being ingested.
Moving the anchor changes the generated data, so the committed `ci` manifest regenerates with
it and `make seed-manifest CHECK=1` compares against the new one. Stated here so that the
pinned anchor reads as a decision that is still correct rather than as something nobody
revisited. M4 found the constraint; `docs/runbook.md` carries it.

**The ECB feed cannot return a rate for a simulated date beyond the real one.** The `ci` profile
pins its anchor, so sixty ticks land past today, and the simulated clock is free to run ahead of
the real one — the only real-clock constraint in the schema is
`core.customers.date_of_birth < current_date`, which a simulated future date does not touch.
Either the anchor is chosen so the extracted window stays behind real time, or the feed
synthesises rates for simulated-future dates and says so. Both are defensible and the choice
should be deliberate.

*Resolved at M4, and the second branch turned out not to exist.* Specification 005's backfill
refuses a window ending after today, because Airflow will not schedule a future logical date,
so the anchor branch was taken for a reason stricter than the FX feed. The ECB is the rates'
publisher of record and Frankfurter the transport that serves them. Specification 006 measured
Frankfurter: asked for a date the ECB has not published, it falls back to the latest earlier
publication and returns HTTP 200 with that publication's date. The feed lands a response only
when the returned date equals the requested one, and logs the date as `absent_no_publication`
otherwise, so a simulated-future date lands nothing.
Synthesis is not merely rejected: the rule that keeps weekends absent makes it impossible.

**Contract-of-the-time replay is open, and owned by specification 006.** Replaying history across
a contract version bump currently means checking out the contract as it stood before the bump
by hand, which is what specification 005's acceptance commit sequence records. Specification
006 selects the contract in force for a batch by the batch's interval instead. Its planning
measured that `meta.contract_version` does not yet hold what that needs: `in_force_from` is the
real wall-clock time the register step first saw a version, not the source time from which the
version applies, and the table stores no contract body, only a version number and a
fingerprint.

*Resolved by specification 006* (ADR 0014, and ADR 0016 for selection). Each contract carries an
authored source-time `in_force_from`, superseded versions stay unedited under `history/`, and the
open step selects the version in force on the day the sender produced the delivery from
`meta.contract_version`, which now keeps the source-time date beside the real-time
`first_seen_at`. Proven by a replay: a fresh stack seeded
at the acceptance anchor and backfilled over the whole sixty-one-day window with both version
bumps already committed ran without a halt and with no file edited, validating `payments`
against version 1 through 2026-08-25 and version 2 from 2026-08-26, and the clearing file against
version 1 through 2026-09-02 and version 2 from 2026-09-03. The committed dates are authored
against one anchor, 2026-07-20, because the scripted drift is anchor-relative; the runbook says
so.
