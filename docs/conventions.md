# Conventions

These are fixed for the lifetime of the project. Changing one is a specification amendment,
not a pull request comment.

## Locked decisions

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

## Schemas

Source side, in Postgres:

- `core` holds the operational entities the bank would run on.
- `ref` holds slow-moving reference data the bank would license or maintain by hand.

Warehouse side, in DuckDB:

- `bronze` is the landed source data, typed but otherwise unchanged.
- `silver` is the conformed model: one row per business entity version.
- `gold` is the dimensional model and the marts built on it.
- `dq` holds data quality check results, one row per check run.
- `ops` holds pipeline state: batch registry, watermarks, run outcomes, freshness.
- `meta` holds contract versions, column lineage and the model catalogue.

## Naming

**Models.** Bronze is `br_<source>__<entity>`, with a double underscore between source and
entity so the source stays readable when the entity name itself contains an underscore, for
example `br_corebank__loan_installments`. Silver is `sl_<entity>`, singular for a concept and
plural for a collection matching the source table name. Gold is `dim_<entity>`,
`fct_<grain>` and `mart_<domain>_<subject>`.

**Columns.** `snake_case`. Business keys keep the source name and the `_id` suffix.
Surrogate keys take the `_sk` suffix and are the hash of the business key, plus the valid-from
timestamp where the entity is SCD2. Booleans read as a statement: `is_active`,
`has_collateral`. Dates end in `_date`, timestamps in `_at`. Money columns end in the
currency treatment they carry: `_amount` is in the transaction currency and travels with a
`_currency` column, `_amount_eur` is converted at the rate of the transaction date.
Platform-generated columns start with an underscore, so a column beginning with `_` is never
sourced from the bank.

**DAG ids.** `ingest_<source>` for extraction into bronze, `transform_<layer>` for dbt runs,
`dq_<scope>` for quality gates, `ops_<purpose>` for maintenance, `gov_<purpose>` for
governance work such as GDPR deletion. The DAG id, the module filename and the Airflow
asset prefix are identical.

**Tests.** A generic dbt test is declared in the model schema file next to the column it
constrains. A singular test lives in `dbt/tests/` and is named for the rule it enforces, for
example `assert_gl_entries_balance_by_day.sql`. Every test declares a severity: `error`
blocks the run, `warn` is recorded in the `dq` schema and reported.

## Commits

Conventional Commits, imperative mood, lower case subject, no trailing period, one logical
change per commit. Types in use: `feat`, `fix`, `docs`, `build`, `ci`, `refactor`, `test`,
`chore`.

No emoji anywhere in the repository, including commit messages.

Commit messages and pull request descriptions carry no attribution trailers: no
`Co-Authored-By` lines, no generated-with notices. `.claude/settings.json` sets this for any
agent run inside the repository; contributors working by hand follow the same rule.
