# nordbank-data-platform

Nordbank is a simulated EU-licensed neobank. This repository is the data platform behind it:
a synthetic core banking system and four external feeds are ingested into a medallion
lakehouse (bronze, silver, gold) on MinIO and DuckDB, orchestrated by Airflow, transformed by
dbt, checked by explicit data quality gates, and consumed by a Power BI semantic model and a
Streamlit application. The platform is built to answer the nineteen numbered questions in
[docs/business_questions.md](docs/business_questions.md), which are the acceptance baseline
for the gold layer.

## Architecture

The diagram is authored at M1 as Mermaid under [docs/diagrams/](docs/diagrams/) and exported
to `docs/diagrams/png/` for embedding here. Until then, the end-to-end description is in
[docs/architecture.md](docs/architecture.md).

## Sources

| Source | Type | Load pattern | Cadence |
|---|---|---|---|
| Core banking (Postgres, synthetic) | Relational OLTP | Incremental by watermark on `updated_at`, soft deletes | Daily |
| ECB FX rates (Frankfurter API) | REST API | Incremental by date, gap-filling for weekends/holidays | Daily |
| Card network settlement files | CSV/JSON in object storage | File-arrival driven, late files reprocess prior partitions | Daily |
| Sanctions / PEP list (OpenSanctions) | Bulk download | Full refresh, versioned snapshot | Weekly |
| Macro indicators (FRED) | REST API | Incremental append | Monthly |

## Layers

| Layer | Holds | Guarantees | Does not do |
|---|---|---|---|
| Bronze | Landed source data, partitioned by ingest date | Typed against a contract with the raw payload retained, direct identifiers tokenised at ingest, append only, immutable once registered, audit columns on every row, contract failures quarantined with a reason | Business logic, joins, renaming, deduplication beyond exact batch replay |
| Silver | Conformed entities at entity grain | One row per business entity version, soft deletes applied, late arrivals ordered, EUR amounts in `DECIMAL(18,4)` with rate provenance, UTC timestamps, SCD2 where the source mutates | Aggregation to a reporting grain |
| Gold | Dimensional model and marts | Declared and tested grain per model, conformed dimensions, rebuildable from silver, BI-tool agnostic | Holding state that cannot be rebuilt. Operational marts are the one exception and read `ops` and `dq` |

Three supporting schemas sit beside them: `dq` for quality check results, `ops` for batch
registry, watermarks and freshness, and `meta` for contracts, lineage and the PII vault.

## Stack

| Concern | Choice | Reason |
|---|---|---|
| Source system | Postgres, synthetically generated | Mutation, soft deletes, late arrivals and schema drift are required to test ingestion (ADR 0003) |
| Lake | MinIO, Parquet | S3 API locally, partitioned by ingest date |
| Warehouse | DuckDB, BigQuery as a second dbt target | Runs on one machine with no account; the second target proves portability (ADR 0002) |
| Orchestration | Airflow 3.x | Asset-driven scheduling; the `warehouse_access` pool serialises every warehouse task, read or write |
| Transformation | dbt | Model naming, tests and documentation as code (ADR 0001) |
| Quality | dbt tests and explicit gates writing to `dq` | Results are data, not log lines |
| Consumption | Power BI, Streamlit | Semantic model over exported Parquet, and a public deployment reading DuckDB read-only (ADR 0004) |
| Tooling | uv, ruff, sqlfluff, pre-commit, GNU make | One entry point per task, pinned lockfile |

## Quickstart

Prerequisites: Docker Desktop with at least 6 GB allocated, Python 3.11,
[uv](https://docs.astral.sh/uv/), GNU make, and Git. On Windows, run make from Git Bash with
make on the PATH; the Makefile sets `SHELL := bash`.

```bash
git clone https://github.com/sardorbek-suyunov/nordbank-data-platform.git
cd nordbank-data-platform
cp .env.example .env
make install       # virtual environment and git hooks
make up            # build if needed, start, wait for every healthcheck
make health        # per-component probe table
make verify-dag    # trigger the health-check DAG from the CLI and verify it
make schema-apply  # source DDL, reference seeds and the column classifications
make seed          # generate and load the synthetic operating history
make seed-verify   # the fourteen coherence invariants, against what was loaded

make warehouse-apply      # the warehouse operational schema, applied inside the stack
make contracts-bootstrap  # a data contract per source entity, from the data dictionary
make backfill FROM=.. TO=..   # tick a day, deliver its files, ingest core and the feeds; resumable
make bronze-stats         # landed and quarantined counts by entity and ingest date
make feeds-acceptance     # specification 006's evidence, after a backfill
make test-offline         # the unit and DAG suites in a container with no network
```

The backfill's range has to end on or before today, and the seed's anchor has to be far
enough back for that: a run's logical date is the simulated day, and Airflow will not schedule
a run whose logical date is in the future. The acceptance history is seeded at 2026-07-20, the
anchor the committed contracts are written for. `docs/runbook.md` gives the procedure and
explains what goes wrong at another anchor.

The first `make up` takes about 6 minutes, most of it building the Airflow image and pulling
images. After that it is about 40 seconds from a `make nuke` and about 35 seconds from a
`make down`. Airflow is then at http://localhost:8080 and MinIO at http://localhost:9001.

`make` with no target lists every target. Services the stack does not run yet report the
milestone that implements them.

## Layout

```
.
├── docs/            architecture, conventions, specs, ADRs, checkpoints, BI and diagrams
├── generator/       synthetic core banking source system and its daily mutation engine
│                    deterministic: seed, anchor date and profile fix the output
├── airflow/         DAGs, plugins and orchestration tests
├── dbt/             bronze, silver and gold models, snapshots, macros, tests, seeds
├── contracts/       per-source data contracts and drift behaviour
├── quality/         data quality check definitions, severities and freshness SLAs
├── infra/           docker compose stack and the BigQuery sandbox in Terraform
├── analytics/       Streamlit application
├── scripts/         scripts invoked by the Makefile, by CI and by hand
└── .github/         CI workflows
```

Every directory carries a README stating its purpose and its ownership boundary.

## Milestones

| ID | Milestone | Status |
|---|---|---|
| M0 | Repository foundation and conventions | done |
| M1 | Local stack: Postgres, MinIO, Airflow | done |
| M2 | Source system DDL and initial historical load | done |
| M3 | Mutation engine: daily change generation | pending |
| M4 | Bronze ingestion, contracts and quarantine | pending |
| M5 | Silver: conformance, SCD2, PII tokenisation | pending |
| M6 | Gold: dimensional model and marts | pending |
| M7 | Data quality gates, reconciliation and ops observability | pending |
| M8 | Governance: lineage, GDPR deletion, access control | pending |
| M9 | Consumption: Power BI semantic model and Streamlit application | pending |
| M10 | Portability: BigQuery target and Terraform | pending |

## Documentation

- [Project state](docs/project_state.md) — start here: what exists today, how to verify it, what is next
- [Architecture](docs/architecture.md) — sources, ingestion patterns, layer contracts, storage, orchestration, security, scale
- [Conventions](docs/conventions.md) — the locked decisions and the naming rules
- [Business questions](docs/business_questions.md) — the nineteen questions gold is measured against
- [Metric definitions](docs/metric_definitions.md) — the exact rule behind every contested term
- [Data dictionary](docs/data_dictionary.md) — entity inventory, grain and load pattern
- [Model inventory](docs/model_inventory.md) — every planned model, its grain, inputs and milestone
- [PII classification](docs/pii_classification.md) — the four column classes and how each is handled
- [Generator realism](docs/generator_realism.md) — every distribution, its parameters, the justification, and what is deliberately unrealistic
- [Runbook](docs/runbook.md) — local setup, endpoints, resource envelope, failures, WSL2 notes
- [Decision records](docs/adr/) — medallion layering, DuckDB, synthetic source, BI tooling,
  PII crypto-shredding, LocalExecutor, declarative Airflow configuration, bronze immutability,
  numbered SQL migrations, sanctions screening through the vault, the COPY loader
- [Specifications](docs/specs/) — numbered specs and the amendment protocol
- [Checkpoints](docs/checkpoints/) — one report per completed milestone

## License

MIT. See [LICENSE](LICENSE).
