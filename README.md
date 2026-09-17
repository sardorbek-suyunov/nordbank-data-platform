# nordbank-data-platform

Nordbank is a simulated EU-licensed neobank. This repository is the data platform behind it:
a synthetic core banking system and four external feeds are ingested into a medallion
lakehouse (bronze, silver, gold) on MinIO and DuckDB, orchestrated by Airflow, transformed by
dbt, checked by explicit data quality gates, and consumed by a Power BI semantic model and a
Streamlit application. The platform is built to answer the eighteen numbered questions in
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
| Bronze | Landed source data, partitioned by ingest date | Typed against a contract, append only, immutable once registered, audit columns on every row, contract failures quarantined with a reason | Business logic, joins, renaming, deduplication beyond exact batch replay |
| Silver | Conformed entities at entity grain | One row per business entity version, soft deletes applied, late arrivals ordered, EUR amounts in `DECIMAL(18,4)`, UTC timestamps, PII tokenised, SCD2 where the source mutates | Aggregation to a reporting grain |
| Gold | Dimensional model and marts | Declared and tested grain per model, conformed dimensions, rebuildable from silver, BI-tool agnostic | Holding state that cannot be rebuilt |

Two supporting schemas sit beside them: `dq` for quality check results and `ops` for batch
registry, watermarks and freshness, with `meta` for contracts and lineage.

## Stack

| Concern | Choice | Reason |
|---|---|---|
| Source system | Postgres, synthetically generated | Mutation, soft deletes, late arrivals and schema drift are required to test ingestion (ADR 0003) |
| Lake | MinIO, Parquet | S3 API locally, partitioned by ingest date |
| Warehouse | DuckDB, BigQuery as a second dbt target | Runs on one machine with no account; the second target proves portability (ADR 0002) |
| Orchestration | Airflow 3.x | Asset-driven scheduling; the `warehouse_write` pool serialises DuckDB writes |
| Transformation | dbt | Model naming, tests and documentation as code (ADR 0001) |
| Quality | dbt tests and explicit gates writing to `dq` | Results are data, not log lines |
| Consumption | Power BI, Streamlit | Semantic model and a public deployment (ADR 0004) |
| Tooling | uv, ruff, sqlfluff, pre-commit, GNU make | One entry point per task, pinned lockfile |

## Quickstart

Prerequisites: Python 3.11, [uv](https://docs.astral.sh/uv/), GNU make, and Git. On Windows,
run make from Git Bash with make on the PATH; the Makefile sets `SHELL := bash`. Docker
Desktop is required from M1 onward, when there is a stack to start.

```bash
git clone https://github.com/sardorbek-suyunov/nordbank-data-platform.git
cd nordbank-data-platform
cp .env.example .env
make install
make lint
make
```

`make` with no target lists every target. At M0 the repository contains documentation,
conventions and tooling only: the targets that need a running service report the milestone
that implements them.

## Layout

```
.
├── docs/            architecture, conventions, specs, ADRs, checkpoints, BI and diagrams
├── generator/       synthetic core banking source system and its daily mutation engine
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
| M1 | Local stack: Postgres, MinIO, Airflow | pending |
| M2 | Source system DDL and initial historical load | pending |
| M3 | Mutation engine: daily change generation | pending |
| M4 | Bronze ingestion, contracts and quarantine | pending |
| M5 | Silver: conformance, SCD2, PII tokenisation | pending |
| M6 | Gold: dimensional model and marts | pending |
| M7 | Data quality gates, reconciliation and ops observability | pending |
| M8 | Governance: lineage, GDPR deletion, access control | pending |
| M9 | Consumption: Power BI semantic model and Streamlit application | pending |
| M10 | Portability: BigQuery target and Terraform | pending |

## Documentation

- [Architecture](docs/architecture.md) — sources, ingestion patterns, layer contracts, storage, orchestration, security, scale
- [Conventions](docs/conventions.md) — the locked decisions and the naming rules
- [Business questions](docs/business_questions.md) — the eighteen questions gold is measured against
- [Data dictionary](docs/data_dictionary.md) — entity inventory, grain and load pattern
- [Runbook](docs/runbook.md) — local setup, endpoints, failures, backfill, escalation
- [Decision records](docs/adr/) — medallion layering, DuckDB, synthetic source, BI tooling
- [Specifications](docs/specs/) — numbered specs and the amendment protocol
- [Checkpoints](docs/checkpoints/) — one report per completed milestone

## License

MIT. See [LICENSE](LICENSE).
