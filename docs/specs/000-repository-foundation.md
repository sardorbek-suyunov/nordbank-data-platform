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

## Amendments

Appended during implementation. The scope text above is left as approved; the protocol is in
`docs/specs/README.md`.

### 2026-09-17 — No `.gitkeep` files

Section 1 asks for `.gitkeep` where a directory must exist with no content, and in the same
paragraph asks for a `README.md` in every directory. The README already makes the directory
tracked, so `.gitkeep` would add a file that does nothing. None is created.

### 2026-09-17 — sqlfluff runs without dbt at M0

Section 2 specifies `sqlfluff-templater-dbt` and section 9 specifies a tolerant `sqlfluff
lint`. The dbt templater cannot run without a dbt project, and dbt project initialisation is
out of scope for this milestone, so the dependency would pull `dbt-core` and `metricflow`
into a milestone with no SQL and still not lint anything.

Changed: the dev group installs plain `sqlfluff`. `.sqlfluff` uses `templater = jinja` with
stub macros for `ref`, `source` and `config` under `[sqlfluff:templater:jinja:macros]`, so dbt
models lint without a dbt install. `make lint` and CI skip sqlfluff when no `.sql` file exists
under `dbt/`, printing an explicit skip message rather than reporting a silent pass. Revisited
at M4, when the dbt project exists and the templater changes to `dbt`.

### 2026-09-17 — No `.github/README.md`

Section 1 lists `.github/workflows/` and section 3 requires a README per directory. GitHub
resolves the repository homepage README in the order `.github/README.md`, root `README.md`,
`docs/README.md`, so a README in `.github/` would replace the project README on the homepage
and break acceptance criterion 7. `.github/` holds no README; the CI setup is documented in
`.github/workflows/README.md`.

### 2026-09-17 — CI documentation checks narrowed and simplified

Section 9 requires the `Status:` check over every file in `docs/adr/` and `docs/specs/`, which
would fail on the purpose READMEs that section 1 requires in those same directories. README
files are exempt from the status check.

The second check is simplified from "no `TODO` marker outside the milestone status table" to
"no `TODO` token anywhere in `README.md`". The milestone table uses `done`, `in progress` and
`pending` as status values and therefore never carries a `TODO` marker, which makes the
positional exception unnecessary and ambiguous to implement.

### 2026-09-17 — Commit count cap removed

Acceptance criterion 9 required between 5 and 9 commits. The cap is removed. A commit must be
a coherent, independently reviewable change; the number of commits is whatever that produces.
The rest of criterion 9 stands: Conventional Commits, imperative mood, one logical change per
commit.

### 2026-09-17 — ADR word budget replaced by a quality bar

Section 4 set a budget of 150 to 300 words per ADR. The budget is removed. A record must
instead name at least one rejected alternative with the reason it lost, and at least one
negative consequence of the option chosen. The four existing ADRs, which ran 307 to 383
words, stand unchanged.

### 2026-09-17 — Agent and editor configuration excluded from the repository (item 2)

Section 2 lists the contents of `.gitignore`. Four patterns are added, covering agent and
editor configuration: the agent configuration directory, its instructions file, `.mcp.json`
and `*.plan.md`; the patterns themselves are in `.gitignore`. Local tooling configuration is
not part of the platform and does not belong in a repository that documents it.

### 2026-09-17 — History rewritten to remove committed configuration (item 3)

One such file had already been committed. History was rewritten with `git filter-repo` to
remove it from every commit, and `main` was force-pushed. Every commit hash from that point in
the history onward changed, so the hashes in the M0 checkpoint are the rewritten ones. The
repository has no collaborators, which is what makes this safe now and impossible later.

### 2026-09-17 — No tool is named anywhere in the repository (item 4)

`docs/conventions.md` named a specific tool configuration file when stating the commit
attribution rule. The sentence is removed. The rule now reads without naming anything: commit
messages and pull request descriptions carry no attribution trailers, no `Co-Authored-By`
lines and no generated-with notices, for every contributor and every tool. No specific tool is
named in any file in this project.

### 2026-09-17 — PII handling replaced with crypto-shredding (item 5)

Section 3 required a PII handling narrative; the delivered version tokenised in silver and
kept raw identifiers in bronze, which cannot satisfy erasure without editing bronze.
Replaced: identifiers are tokenised at ingest, before anything reaches the lake, and the
reversible mapping lives in a vault table in `meta`. Erasure deletes the vault entry, leaving
bronze byte-identical and permanently unresolvable. Erasure is irreversible and published
aggregates are not retracted. Recorded in full as ADR 0005, which raises the ADR count in
section 4 from four to five.

### 2026-09-17 — Watermark extraction uses an explicit overlap (item 6)

Section 6 describes incremental extraction by watermark on `updated_at`. The predicate is
`updated_at >= watermark - EXTRACT_LAG`, default 15 minutes, not `> watermark`. It defends
against rows sharing the boundary `updated_at` and rows committed after the watermark was
taken. The duplicates it produces are absorbed by idempotent deduplication in silver.

### 2026-09-17 — Hard deletes documented as a limitation (item 7)

Watermark extraction cannot detect a physical delete in the source. Added as an explicit
limitation, with the mitigation: a scheduled full primary-key reconciliation diffing source
keys against silver and reporting orphans to `dq`, implemented at M7.

### 2026-09-17 — Bronze fidelity reconciled with typing (item 8)

Typed against the contract and what is stored is what the source sent contradicted each other.
Resolved: for API and file sources bronze retains the raw payload alongside the parsed
columns, a failed cast routes the row to quarantine with the reason and is never coerced to
null, and the source row count is always reconstructable as landed plus quarantined.

### 2026-09-17 — Operational marts read ops and dq (item 9)

The gold contract said gold is rebuilt from silver and holds no state of its own. Amended:
gold is rebuilt from silver, except the operational marts in the `ops` domain, which read
`ops` and `dq` by design because the data they report on exists nowhere else.

### 2026-09-17 — Currency conversion is as-of and never restated (item 10)

Conversion is an as-of join taking the latest rate where `rate_date <= transaction_date`, with
no restatement once the true same-day rate publishes, and every converted fact carries
`fx_rate`, `fx_rate_date` and `fx_is_carried`. The ECB publishes mid-afternoon CET, so
late-day transactions have no same-day rate at ingest, and silent restatement would make an
already published report irreproducible.

### 2026-09-17 — Schema drift behaviour defined in the bronze contract (item 11)

Added to the bronze layer contract: additive source columns are accepted and logged to `meta`;
a type change, a removed column or a primary-key change quarantines the whole batch and fails
the ingestion gate, rather than loading part of it.

### 2026-09-17 — Three scale profiles replace two (item 12)

Section 3 specified an environment strategy of `dev` and `full`. Replaced with `ci` (about 500
customers, tens of thousands of transactions, full run under a minute), `dev` (about 5,000
customers, a few million transactions, the default) and `full` (about 250,000 customers, tens
of millions of transactions). `.env.example` is updated. The rule that no model, DAG or
contract is conditional on the profile is unchanged.

### 2026-09-17 — Consumption path made concrete (item 13)

Power BI reads gold as Parquet exported to `exports/` through a folder source until M10, then
through the BigQuery native connector once the second dbt target exists. Streamlit opens the
DuckDB file read-only. DuckDB has no first-party Power BI connector, which the previous text
left unsaid.

### 2026-09-17 — Silver naming rule made deterministic (item 14)

`sl_<entity>` was ambiguous about singular and plural. Silver model names are plural and
mirror the source entity name exactly (`sl_customers`, `sl_loan_installments`). Dimensions are
singular (`dim_customer`). Facts are named for their grain (`fct_transactions`,
`fct_account_balance_daily`).

### 2026-09-17 — Numeric precision rule added (item 15)

Section 5 fixed money at `DECIMAL(18,4)` and said nothing about rates. Added: exchange rates
and interest rates are `DECIMAL(18,8)`, and percentages are stored as decimal fractions, never
as values from 0 to 100.

### 2026-09-17 — Reserved dimension members added (item 16)

Every dimension carries surrogate key `-1` for Unknown and `-2` for Not applicable. Facts join
dimensions with a left join and coalesce to those members, so a fact row is never dropped or
silently orphaned by a missing dimension record.

### 2026-09-17 — SCD2 join policy added (item 17)

A fact resolves its dimension surrogate key as of the event timestamp using `_valid_from` and
`_valid_to`. Every SCD2 dimension also exposes the natural business key as a durable key for
current-state slicing and for Power BI relationships on latest state. Both columns are
mandatory.

### 2026-09-17 — Primary key testing rule added (item 18)

Every model declares its primary key and tests it with `unique` and `not_null`, or documents
an exemption in its schema file with the reason.

### 2026-09-17 — Rate provenance required beside converted amounts (item 19)

Wherever an `_amount_eur` column exists, `fx_rate` and `fx_rate_date` exist beside it. A
converted amount with no visible rate provenance is a defect.

### 2026-09-17 — Metric definitions added as a separate document (item 20)

Added `docs/metric_definitions.md`, referenced from `docs/business_questions.md`: one entry per
contested term with the exact rule, the source columns and the questions that consume it.
Terms that genuinely cannot be decided yet, the inter-regional interchange rates and the
funding cost assumption, are marked as undecided and to be resolved before M6, with what the
decision depends on.

### 2026-09-17 — Nineteenth business question added (item 21)

`login_sessions` was ingested and consumed by nothing. Added question 19 to the Fraud and AML
group: share of transactions preceded by a login from an unrecognised device within 24 hours,
by channel and month, persona Head of Fraud, mart `mart_fraud_device_risk`, grain one row per
month and channel. Acceptance criterion 5 changes from 18 questions to 19.

### 2026-09-17 — Coverage rule added (item 22)

Added to `docs/business_questions.md`: every source entity feeds at least one silver model, and
every silver model either feeds a gold model or is documented as reference-only. An
unreferenced entity is a design defect, not an accident.

### 2026-09-17 — agent_locations replaces branches (item 23)

Section 7 listed `branches`. A licensed neobank has no branch network, so the entity described
something the business does not have. Replaced with `agent_locations`, the cash-in and
cash-out partner network, which gives cash transactions a geography and supports the
structuring analysis in question 11. The entity count in acceptance criterion 6 stays at 18.

### 2026-09-18 — Exclusion of agent configuration moves out of the tracked ignore file (item G1)

The previous amendment added four agent and editor patterns to `.gitignore`, which made
`.gitignore` the one tracked file naming a specific tool. Reverted: those patterns are removed
from `.gitignore`, which keeps `.notes/`, and live instead in `.git/info/exclude` for this
repository and in the global `core.excludesfile` for every repository. Both are untracked. The
repository names no tool in any tracked file, and the mechanism that achieves it is an
untracked exclude file.

### 2026-09-18 — Remote repository recreated to drop unreachable objects (item G2)

A force-push does not remove anything from GitHub. The pre-rewrite commit and the file it
contained were still served by SHA through the API, the raw endpoint and the blob URL after
the rewrite. Rewriting history is therefore only half of a removal on a hosted repository; the
other half is deleting and recreating the remote. Recorded so that the next removal starts
from the correct assumption.

### 2026-09-18 — Quarantine stores tokens for identifier columns (item G4)

The bronze contract sent the raw failing value to quarantine, which placed cleartext
identifiers in a mutable table outside the vault, where erasure could not reach them. Amended:
for a column classified as an identifier, quarantine stores the token, the failing column name
and the failure reason. Quarantine tables are inside the scope of the erasure workflow.

### 2026-09-18 — Bronze payload is structurally faithful, not byte-identical (item G5)

`_raw_payload` was defined as the record as received, which preserved cleartext identifiers
and defeated crypto-shredding for every API and file source. Amended: the payload keeps the
shape, field names, ordering and every non-identifier value exactly as sent, with identifier
fields carrying their tokens. The consequence is stated in the architecture and added to ADR
0005: bronze is structurally faithful rather than byte-identical, and reconstructing an
original record requires the vault.

### 2026-09-18 — Silver generalises quasi-identifiers rather than resolving tokens (item G6)

The silver contract said silver resolves tokens to the coarse attributes reporting needs,
which a keyed hash cannot do. Amended: identifiers stay tokenised and are never resolved in
silver; the attributes reporting needs are derived by generalising quasi-identifiers, such as
an age band from the date of birth and a country from the address.

### 2026-09-18 — PII classification taxonomy added (item G7)

Added `docs/pii_classification.md` with four classes and their handling rules: `identifier`,
`quasi-identifier`, `sensitive` and `non-personal`. Every column in every contract carries a
classification from M2 onward. Referenced from `architecture.md` and `conventions.md`. Date of
birth is documented as the case that proves the distinction, since tokenising it would make
age banding impossible while protecting almost nothing.

### 2026-09-18 — Missing exchange rate defined (item G8)

The conversion rule did not say what happens when no rate exists at or before the transaction
date. Amended: `amount_eur` is null, `fx_is_missing` flags the row, and a `dq` check of
severity `error` fires. The value is never zero and never the unconverted amount.

### 2026-09-18 — Rerun semantics made precise (item G9)

Bronze immutability was stated without saying when a partition becomes immutable. Amended: a
task retry inside an unregistered batch overwrites its own partial output; after registration
the partition is final, and a rerun creates a new batch id and a new partition, with silver
deduplicating the overlap. Immutability is a property of registered partitions.

### 2026-09-18 — `account_holders` bridge entity added (item G10)

The metric definitions relied on an account-to-holder relationship that no entity provided.
Added `account_holders`: one row per account and holder, with a holder role and an ownership
weighting factor summing to one per account. Per-customer monetary measures split by the
weighting factor while customer counts count each holder once. The entity inventory moves from
18 entities to 19, and acceptance criterion 6 changes from 18 to 19.

### 2026-09-18 — Model inventory added and the coverage rule made bidirectional (item G11)

Added `docs/model_inventory.md`: every planned seed, silver model, dimension, bridge, fact,
mart, platform table and derived flag, with its grain, upstream inputs, the questions it serves
and the milestone that builds it. `is_customer_initiated` gains its own entry in the metric
definitions, naming exactly which transaction types are customer-initiated. The coverage rule
now runs in both directions: no source entity without a consumer, and no referenced object
without a home.

### 2026-09-18 — Settlement reconciliation compares at source precision (item G12)

The break rule converted both sides to EUR before comparing, which manufactures breaks through
rounding and compares a number the network sent with one the platform computed. Amended:
comparison is per settlement date, network and settlement currency, at source precision, with
zero detection tolerance. EUR conversion is used only to report the size of a break. The
materiality thresholds, 0.1 per cent of the file total and 100 units, are expressed in the
settlement currency.

### 2026-09-18 — `info` severity defined in the conventions (item G13)

The metric definitions used an `info` severity that the conventions did not define. Amended:
the conventions now carry all three levels, `error`, `warn` and `info`, with the same meanings
the metric definitions use.

### 2026-09-18 — Three precision fixes (item G14)

The interest day-count convention is named as Actual/365 Fixed. The ECB publication time is
stated as around 16:00 CET, which is 15:00 or 14:00 UTC depending on daylight saving, and the
freshness window for that source is derived from the CET time rather than a fixed UTC hour.
The `full` profile carries a note that it cannot be seeded row by row and requires `COPY`-based
bulk loading, as a constraint on the M2 and M3 generator design.
