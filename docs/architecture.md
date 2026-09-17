# Architecture

Nordbank is a simulated EU-licensed neobank. The platform ingests a synthetic core banking
system and four external feeds into a medallion lakehouse, transforms them with dbt, and
serves a dimensional model to Power BI and a Streamlit application. Everything runs on one
machine; the cloud footprint exists only to prove the transformation layer is portable.

## Sources

| Source | Type | Load pattern | Cadence |
|---|---|---|---|
| Core banking (Postgres, synthetic) | Relational OLTP | Incremental by watermark on `updated_at`, soft deletes | Daily |
| ECB FX rates (Frankfurter API) | REST API | Incremental by date, gap-filling for weekends/holidays | Daily |
| Card network settlement files | CSV/JSON in object storage | File-arrival driven, late files reprocess prior partitions | Daily |
| Sanctions / PEP list (OpenSanctions) | Bulk download | Full refresh, versioned snapshot | Weekly |
| Macro indicators (FRED) | REST API | Incremental append | Monthly |

### Ingestion pattern per source

**Core banking.** Extraction reads each entity where `updated_at > watermark`, where the
watermark is the highest `updated_at` successfully committed for that entity, stored in
`ops`. The watermark advances only after the parquet partition is written and registered, so
a failed run repeats rather than skips. Deletes are soft: the source sets `is_deleted` and
`updated_at`, the row arrives through the normal incremental path, and the delete is applied
in silver rather than by removing a bronze row. Rows whose `updated_at` is older than the
partition date they arrive in are late-arriving and land in the partition of their ingest
date, keeping bronze append-only, while silver resolves order by `updated_at`.

**ECB FX rates.** The API is queried for the date range between the last loaded rate date and
the run date. The ECB publishes on working days only. Weekend and holiday gaps are filled by
carrying the last published rate forward into a rate table that has one row per currency per
calendar date, because conversion must not fail on a Sunday transaction. Carried rows are
flagged so that the fill is visible downstream.

**Card network settlement files.** Files land under a bucket prefix and a sensor triggers the
DAG on arrival rather than on a clock. A file names the settlement date it covers, which may
be earlier than the arrival date. Late files therefore reprocess the partition of their
settlement date, and the reconciliation mart is rebuilt for the affected dates.

**Sanctions and PEP list.** The published snapshot is downloaded whole each week and stored
under a version derived from the publication date. Screening results reference the version
they were produced against, so a past decision can be explained with the list as it stood
that day.

**Macro indicators.** Series are appended monthly. Revisions to already published periods are
expected; the load is an upsert keyed on series and period, and the previous value is kept in
the silver history.

## Layer contracts

**Bronze guarantees** that what is stored is what the source sent. Data is typed against the
contract in `contracts/` and nothing else: no renaming beyond casing, no joins, no business
rules, no deduplication except on the exact replay of a batch. Bronze is append-only and
immutable once a partition is registered; a correction is a new batch, never an edit. Every
row carries `_ingested_at`, `_source_file`, `_batch_id` and `_source_system`. Rows that fail
the contract are written to a quarantine table with the failure reason instead of being
dropped, so the row count of the source is always reconstructable.

**Silver guarantees** one row per business entity per version. It applies deduplication on the
business key, resolves late arrivals by `updated_at`, applies soft deletes, converts amounts
to `DECIMAL(18,4)` with an EUR equivalent at the transaction date rate, normalises timestamps
to UTC, and tokenises PII. Entities that mutate are SCD2 with `_valid_from`, `_valid_to` and
`_is_current`. Joins across sources are allowed. Aggregation to a reporting grain is not:
silver stays at entity grain so that a mart can be rebuilt without re-ingesting.

**Gold guarantees** a declared grain per model, tested. Dimensions are conformed and keyed by
surrogate key, facts carry foreign keys to those dimensions and measures at a stated grain,
and marts answer the numbered questions in `docs/business_questions.md`. Gold is rebuilt from
silver, holds no state of its own, and stays free of shaping that exists only to suit one BI
tool.

## Storage layout

Raw extracts land in MinIO under the bucket `nordbank-lake`:

```
bronze/<source>/<entity>/ingest_date=YYYY-MM-DD/part-*.parquet
```

Partitioning is by ingest date rather than business date, so that a partition is written
once and never revisited. Business date filtering happens in silver, where late arrivals can
be ordered correctly.

The warehouse is a single DuckDB file holding the `bronze`, `silver`, `gold`, `dq`, `ops` and
`meta` schemas. Bronze tables read from the parquet files; from silver upward the data is
materialised in DuckDB.

## Orchestration

Airflow 3.x with asset-driven scheduling. Ingestion DAGs produce assets named for the entity
they land. Transformation DAGs consume those assets, so silver runs when its inputs are
present rather than on a timer that hopes they are. Quality gates run after the layer they
check and publish their results as assets in turn, which lets a mart wait on a passing gate.

DuckDB allows a single writer. Every task that writes to the warehouse acquires the Airflow
pool `warehouse_write`, which has one slot, and every reader opens a read-only connection.
This is the reason the pool exists and it is recorded in ADR 0002.

Pipeline state lives in the `ops` schema: a batch registry with one row per extraction batch,
per-entity watermarks, run outcomes, and freshness measurements per source. Reruns are keyed
on the batch id, so a repeated run replaces its own output and nothing else.

## Consumption

Power BI holds the semantic model: relationships, measures in DAX, and the executive reports.
It reads gold and nothing below it.

The Streamlit application is the publicly deployable face of the project and the operational
view: pipeline state, freshness, quality results, and a small number of analytical screens.
It connects read-only.

Both read the same gold models. A measure that matters is defined in gold, not in a report,
so the two tools cannot disagree.

## Security and PII

The synthetic source contains realistic personal data by construction: names, addresses,
dates of birth, national identifiers and device fingerprints. It is generated, not real, but
the platform handles it as if it were not.

Direct identifiers are tokenised on entry to silver with a keyed hash. The salt is supplied
by `PII_TOKEN_SALT` and is never committed. Bronze keeps the source values because bronze is
the immutable record of what arrived; access to bronze is separate from access to gold, and
gold exposes tokens and coarse attributes such as country and age band rather than raw
identifiers.

GDPR deletion is a governance DAG rather than a manual query: a deletion request maps a
customer to every token derived from it and rewrites the affected silver partitions, leaving
an auditable record in `meta`. That work is M8.

No credential is stored in the repository. All configuration arrives through environment
variables documented in `.env.example`, and `.env` is gitignored and blocked by a pre-commit
hook.

## Environments and scale

Two profiles, selected by `NORDBANK_ENV`:

- `dev` generates a reduced dataset, in the order of thousands of customers and a few
  million transactions, so that a full pipeline run finishes on a laptop in minutes. This is
  the default and the profile CI would use.
- `full` generates the complete history, in the order of tens of millions of transactions,
  to exercise incremental logic, partition pruning and the single-writer constraint under
  load.

The two profiles differ only in generator parameters and dbt variables. No model, DAG or
contract is conditional on the profile; if a transformation only works at small scale, that
is a defect, not a configuration.
