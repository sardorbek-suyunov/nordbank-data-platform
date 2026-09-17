# 000 — Repository Foundation and Conventions

Status: Approved
Depends on: —

## Goal
Establish the repository skeleton, engineering conventions, architecture
documentation and requirements baseline for the Nordbank data platform, so that
all later work has a fixed target to build against. No runtime services and no
pipeline code in this milestone.

## Context
Nordbank is a simulated EU-licensed neobank. The platform ingests a synthetic
core banking system plus four external feeds into a medallion lakehouse
(bronze/silver/gold) orchestrated by Airflow, transformed by dbt, stored in
DuckDB, and consumed by Power BI and a Streamlit application.

## Scope

### 1. Repository structure
Create the following directories, each containing a `README.md` of 3–10 lines
stating its purpose and ownership boundary. Use `.gitkeep` where a directory
must exist but has no content yet.

.
├── docs/
│   ├── adr/
│   ├── checkpoints/
│   ├── specs/
│   ├── bi/
│   └── diagrams/
├── generator/
├── airflow/
│   ├── dags/
│   ├── plugins/
│   └── tests/
├── dbt/
│   ├── models/{bronze,silver,gold}/
│   ├── snapshots/
│   ├── macros/
│   ├── tests/
│   └── seeds/
├── contracts/
├── quality/
├── infra/
│   ├── docker/
│   └── terraform/
├── analytics/streamlit_app/
├── scripts/
└── .github/workflows/

### 2. Tooling and configuration
- `pyproject.toml`: Python 3.11, project metadata, dev dependency group
  (`ruff`, `sqlfluff`, `sqlfluff-templater-dbt`, `pytest`, `pre-commit`).
  Managed with `uv`; commit `uv.lock`.
- `ruff.toml` or `[tool.ruff]`: line length 100, target py311, rules
  `E,F,I,N,UP,B,SIM,PTH`.
- `.sqlfluff`: dialect `duckdb`, templater `dbt`, capitalisation policy lower
  for keywords, 4-space indent, max line length 120.
- `.pre-commit-config.yaml`: ruff, ruff-format, sqlfluff-lint,
  end-of-file-fixer, trailing-whitespace, check-yaml, check-merge-conflict,
  detect-private-key, and a hook blocking `.env` from being committed.
- `.gitignore`: Python, uv, dbt (`target/`, `dbt_packages/`, `logs/`), DuckDB
  (`*.duckdb`, `*.duckdb.wal`), Airflow (`logs/`, `airflow.db`), `.env`,
  `.notes/`, IDE files, `exports/`, `data/`.
- `.env.example`: every environment variable the platform will need, with safe
  placeholder values and an inline comment per variable. Never commit `.env`.
- `Makefile`: self-documenting (`make` with no target prints help via a
  `##`-comment convention). Targets required now, may be stubbed with an
  explicit `not implemented until M<n>` message where the dependency does not
  yet exist:
  `help`, `install`, `lint`, `format`, `test`, `up`, `down`, `health`,
  `logs`, `seed`, `tick`, `dbt-build`, `dbt-docs`, `dq`, `clean`.
- `LICENSE`: MIT.

### 3. Documentation
- `README.md` — sections in this order: one-paragraph project statement;
  architecture diagram placeholder; source inventory table; layer
  responsibilities table; tech stack table; quickstart; repository layout;
  milestone status table; links to key docs. No emoji. No superlatives.
- `docs/architecture.md` — end-to-end narrative: sources, ingestion patterns
  per source, layer contracts (what is guaranteed at bronze/silver/gold),
  storage layout, orchestration model, consumption layer, security and PII
  handling, environment/scale strategy (`dev`/`full`).
- `docs/conventions.md` — the locked conventions table (below), naming rules
  for schemas, models, DAG ids, columns, surrogate keys, tests and commits.
- `docs/business_questions.md` — the numbered requirements list (below),
  grouped by domain, each with: question, owning persona, target mart, and
  grain required. This is the acceptance baseline for the gold layer.
- `docs/data_dictionary.md` — headed table with the entity inventory (below),
  one row per entity: name, domain, grain, load pattern, notes. Column-level
  detail is deferred to spec 002.
- `docs/runbook.md` — skeleton with empty sections: local setup, service
  endpoints, common failures, backfill procedure, escalation.
- `docs/diagrams/README.md` — states that diagrams are authored as Mermaid in
  Markdown and exported to PNG only for the README.

### 4. Architecture Decision Records
One file each, format: Title / Status / Context / Decision / Consequences /
Alternatives considered. Substance over length; 150–300 words each.
- `0001-medallion-architecture.md` — why bronze/silver/gold over a single
  transformation layer; what each layer guarantees; immutability of bronze.
- `0002-duckdb-as-warehouse.md` — why DuckDB for local-first analytics; the
  single-writer constraint and how it will be handled (all warehouse writes
  serialised through an Airflow pool `warehouse_write` with one slot; readers
  use read-only connections); BigQuery retained as a second dbt target to
  prove portability.
- `0003-synthetic-source-system.md` — why a generated core banking system
  rather than a static public dataset: required to demonstrate incremental
  extraction, updates, soft deletes, late-arriving data and schema drift.
  Which real external datasets are used alongside it and why.
- `0004-bi-tooling.md` — Power BI for the semantic model and executive
  reporting, Streamlit for the public deployment and operational tooling;
  Metabase explicitly rejected; the gold layer must remain BI-tool agnostic.

### 5. Locked conventions (reproduce in docs/conventions.md)
| Item | Decision |
|---|---|
| Base currency | EUR |
| Timezone | UTC; all timestamps `timestamptz`; naive datetimes prohibited |
| Money type | `DECIMAL(18,4)`; floating point prohibited for monetary values |
| Python | 3.11, `uv`, pinned lockfile |
| Orchestrator | Airflow 3.x, asset-driven scheduling |
| Warehouse | DuckDB primary, BigQuery sandbox secondary |
| Lake | MinIO bucket `nordbank-lake`; `bronze/<source>/<entity>/ingest_date=YYYY-MM-DD/part-*.parquet` |
| Source schemas | `core`, `ref` |
| Warehouse schemas | `bronze`, `silver`, `gold`, `dq`, `ops`, `meta` |
| Model naming | `br_<source>__<entity>`, `sl_<entity>`, `dim_<entity>`, `fct_<grain>`, `mart_<domain>_<subject>` |
| DAG ids | `ingest_<source>`, `transform_<layer>`, `dq_<scope>`, `ops_<purpose>`, `gov_<purpose>` |
| Surrogate keys | `_sk` suffix, hashed from business key(s); business keys keep `_id` |
| Audit columns | bronze: `_ingested_at`, `_source_file`, `_batch_id`, `_source_system`; silver: `_valid_from`, `_valid_to`, `_is_current` on SCD2 |
| Commits | Conventional Commits, imperative mood, one logical change per commit |

### 6. Source inventory (reproduce in README and architecture.md)
| Source | Type | Load pattern | Cadence |
|---|---|---|---|
| Core banking (Postgres, synthetic) | Relational OLTP | Incremental by watermark on `updated_at`, soft deletes | Daily |
| ECB FX rates (Frankfurter API) | REST API | Incremental by date, gap-filling for weekends/holidays | Daily |
| Card network settlement files | CSV/JSON in object storage | File-arrival driven, late files reprocess prior partitions | Daily |
| Sanctions / PEP list (OpenSanctions) | Bulk download | Full refresh, versioned snapshot | Weekly |
| Macro indicators (FRED) | REST API | Incremental append | Monthly |

### 7. Entity inventory (reproduce in docs/data_dictionary.md)
`customers`, `customer_addresses`, `accounts`, `cards`, `merchants`,
`mcc_codes`, `transactions`, `payments`, `loans`, `loan_applications`,
`loan_installments`, `gl_entries`, `fraud_alerts`, `login_sessions`,
`products`, `branches`, `fx_rates`, `sanctions_entities`.
For each: domain, grain, expected load pattern, one-line purpose.

### 8. Business questions (reproduce in docs/business_questions.md)
Growth & customer
1. Monthly active accounts and month-over-month growth by country and product.
2. Cohort retention of customers by signup month over the first 12 months.
3. Total customer deposit balance trend by currency and account type.
Revenue
4. Interchange revenue by MCC category, country and month.
5. Net interest income proxy on the loan book by product and vintage.
6. Fee and interchange revenue per active customer per month.
Credit risk
7. 30/60/90-day delinquency rate by origination vintage.
8. Loan approval rate and subsequent default rate by applicant risk band.
9. Loan lifecycle duration: application to disbursement to first payment.
Fraud & AML
10. Alert precision and false-positive rate by detection rule and month.
11. Customers with multiple sub-threshold cash deposits within a rolling
    7-day window (structuring candidates).
12. Customers transacting with counterparties matched to the sanctions list.
13. Cross-border payment volume and decline rate by corridor.
Treasury & control
14. Net FX exposure by currency, daily.
15. Settlement breaks: card network file totals versus internal ledger totals.
16. Daily general ledger integrity: total debits equal total credits.
Data operations
17. Freshness SLA compliance per source per day.
18. Data quality check pass rate trend by layer and severity.

## Out of scope
docker-compose and any running service; source DDL; column-level data
dictionary; generator logic; DAGs; dbt project initialisation; Terraform;
Streamlit application; Power BI artifacts.

## Acceptance criteria
1. `make` with no arguments prints a readable target list.
2. `uv sync && make lint` passes locally and in CI.
3. Every directory listed in section 1 exists and contains a purpose README.
4. All four ADRs exist, follow the stated format, and state consequences.
5. `docs/business_questions.md` contains all 18 numbered questions with
   persona, target mart and grain.
6. `docs/data_dictionary.md` lists all 18 entities.
7. `README.md` renders correctly on GitHub, contains no emoji, and its
   milestone table shows M0 complete and M1–M10 pending.
8. `.env.example` exists; `.env` is gitignored; no secrets in the repository.
9. Commit history contains between 5 and 9 commits, each Conventional Commit
   format, each independently coherent.
10. `docs/checkpoints/M0-summary.md` exists and documents deferred items.
