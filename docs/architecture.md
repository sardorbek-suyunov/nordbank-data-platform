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

**Core banking.** Extraction reads each entity where

```
updated_at >= watermark - EXTRACT_LAG
```

where `watermark` is the highest `updated_at` successfully committed for that entity, stored
in `ops`, and `EXTRACT_LAG` is a configured overlap with a default of 15 minutes. The
comparison is deliberately `>=` with an overlap rather than `>`, because a strict greater-than
loses rows in two situations:

- Several rows share the exact `updated_at` that became the watermark. A strict comparison
  reads one of them and skips the rest for good.
- A row is written inside a transaction that started before the watermark was taken and
  commits after it. Its `updated_at` is older than the watermark by the time it becomes
  visible, so it is never in range again.

The overlap re-reads a short window on every run, which produces duplicate rows. That is
safe because deduplication in silver is idempotent: rows are keyed on the business key and
`updated_at`, and re-processing the same row yields the same silver state. The watermark
advances only after the partition is written and registered, so a failed run repeats rather
than skips.

Deletes are soft. The source sets `is_deleted` and moves `updated_at`, the row arrives
through the normal incremental path, and the delete is applied in silver rather than by
removing a bronze row.

Late-arriving rows land in the partition of their ingest date, which keeps bronze
append-only, and silver resolves order by `updated_at`.

*Known limitation.* Watermark extraction cannot detect a physical delete: a row removed with
`DELETE` leaves no trace in the source to extract, so silver keeps an entity the bank no
longer has. The mitigation is a scheduled full primary-key reconciliation that reads the key
set of each source entity, diffs it against the current silver key set, and reports orphans
to `dq` for investigation rather than deleting them automatically. It runs on its own
cadence, independent of the daily incremental load, and is implemented at M7 with the rest of
the reconciliation work.

**ECB FX rates.** The API is queried for the date range between the last loaded rate date and
the run date. The ECB publishes on working days only. Weekend and holiday gaps are filled by
carrying the last published rate forward into a rate table with one row per currency per
calendar date, because conversion must not fail on a Sunday transaction. Carried rows are
flagged, so the fill is visible downstream.

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

**Bronze guarantees** that what is stored is what the source sent, with direct identifiers
already replaced by tokens (see Security and PII below).

Fidelity and typing are reconciled as follows. For API and file sources, bronze stores the
raw payload of the record, as received, in a `_raw_payload` column alongside the parsed and
typed columns. The parsed columns are a convenience; the payload is the evidence. For the
relational source, the row as extracted is the payload.

Typing is applied against the contract in `contracts/`, and a value that fails its cast is
never coerced to null and never silently dropped: the row is written to the quarantine table
for that entity with the failing column, the raw value and the failure reason. The source row
count is therefore always reconstructable as landed rows plus quarantined rows, for every
batch.

Beyond typing, bronze applies nothing: no renaming past casing, no joins, no business rules,
no deduplication except on the exact replay of a batch. Bronze is append-only and immutable
once a partition is registered; a correction is a new batch, never an edit. Every row carries
`_ingested_at`, `_source_file`, `_batch_id` and `_source_system`.

*Schema drift.* An additive change is accepted: a new source column is loaded, and its
appearance is recorded in `meta` with the batch that introduced it. A type change, a removed
column, or a change to the primary key is not accepted: the whole batch is quarantined and
the ingestion gate fails, so a partially conforming load never reaches bronze. Resolving the
drift means a new contract version and an explicit rerun.

**Silver guarantees** one row per business entity per version. It applies deduplication on the
business key, resolves late arrivals by `updated_at`, applies soft deletes, converts amounts
to `DECIMAL(18,4)` with an EUR equivalent, normalises timestamps to UTC, and resolves tokens
to the coarse attributes reporting needs. Entities that mutate are SCD2 with `_valid_from`,
`_valid_to` and `_is_current`. Joins across sources are allowed. Aggregation to a reporting
grain is not: silver stays at entity grain, so a mart can be rebuilt without re-ingesting.

**Gold guarantees** a declared grain per model, tested. Dimensions are conformed and keyed by
surrogate key, facts carry foreign keys to those dimensions and measures at a stated grain,
and marts answer the numbered questions in `docs/business_questions.md`. Gold is rebuilt from
silver and holds no state of its own.

The one exception is the operational marts, `mart_ops_freshness` and `mart_ops_quality`, which
read `ops` and `dq` by design. They report on the platform rather than on the bank, and the
data they need exists only in those schemas. The exception is deliberate and limited to marts
in the `ops` domain.

## Currency conversion

Every monetary fact carries its original amount and currency, and an EUR equivalent. The
conversion is an as-of join: for a fact dated `transaction_date`, take the rate with the
greatest `rate_date` where `rate_date <= transaction_date`.

The rate is never restated. When the true same-day rate publishes later, facts already
converted keep the rate they were converted with.

This is a choice about reproducibility rather than about precision. The ECB publishes
reference rates in the mid-afternoon CET, so a transaction late in the European day has no
same-day rate when it is ingested. Restating those facts the next morning would change
totals in a report that was already read and circulated, and yesterday's figure would no
longer be reproducible from yesterday's data. Converting as-of and leaving the result alone
keeps every published number explainable.

Provenance is mandatory rather than optional: every converted fact carries `fx_rate`,
`fx_rate_date` and `fx_is_carried`, so a reader can see which rate was used, when it was
published, and whether it was carried forward across a weekend or a holiday.

## Storage layout

Raw extracts land in MinIO under the bucket `nordbank-lake`:

```
bronze/<source>/<entity>/ingest_date=YYYY-MM-DD/part-*.parquet
```

Partitioning is by ingest date rather than business date, so a partition is written once and
never revisited. Business date filtering happens in silver, where late arrivals can be
ordered correctly.

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

Power BI holds the semantic model: relationships, DAX measures, and the executive reports. It
reads gold and nothing below it. The connection is a folder source over Parquet: the gold
models are exported to `exports/` by a dedicated task and Power BI reads that folder. DuckDB
has no first-party Power BI connector, and a single-writer file is the wrong thing to point a
refreshing report at. From M10, when the BigQuery target exists, Power BI connects to
BigQuery natively and the export path becomes the local fallback rather than the primary
route.

The Streamlit application is the publicly deployable face of the project and the operational
view: pipeline state, freshness, quality results, and a small number of analytical screens.
It opens the DuckDB file read-only and never triggers a pipeline run.

Both read the same gold models. A measure that matters is defined in gold, not in a report,
so the two tools cannot disagree.

## Security and PII

The synthetic source contains realistic personal data by construction: names, addresses,
dates of birth, national identifiers and device fingerprints. It is generated, not real, but
the platform handles it as if it were not.

The approach is crypto-shredding.

Direct identifiers are tokenised on entry to bronze, in the extraction task, before anything
is written to the lake. The token is a keyed hash of the identifier, so the same identifier
always produces the same token and joins still work. The reversible mapping from token to raw
value lives in a single vault table in the `meta` schema, which is the only place in the
platform where a raw identifier exists after ingestion. The vault is written by the
extraction task and read by nothing that serves reporting.

Erasure of a subject is the deletion of that subject's vault entries. Bronze is untouched and
stays byte-identical, which is what makes it an immutable record, but the tokens belonging to
that subject can no longer be resolved to a person by anyone, including the operator. The
personal data is gone in the sense that matters, because what remains is a hash with no
surviving key material.

The trade-offs are explicit:

- Erasure is irreversible. There is no undo, no archive copy of the vault row, and a
  mistaken erasure cannot be repaired by reloading, because reloading the source would create
  a new mapping only if the source still holds the subject, which after a legitimate erasure
  it does not.
- Aggregates already published are not retracted. A count, a sum or a report that included
  the subject before erasure keeps its value. The platform does not rewrite history in gold
  and does not attempt to make past reports disagree with themselves.
- The vault is a concentration of risk. Everything the tokenisation protects depends on the
  vault and the key being handled properly, which makes access to `meta` a separate and more
  restricted thing than access to the warehouse.

The token key is supplied by `PII_TOKEN_SALT` and is never committed. Gold exposes tokens and
coarse attributes such as country and age band, never raw identifiers.

The erasure workflow itself is a governance DAG (`gov_erasure`), implemented at M8, which
records each erasure in `meta` with its request, timestamp and the tokens affected, so the
platform can prove what it did without retaining what it erased.

The rationale, the rejected alternatives and the full set of consequences are in ADR 0005.

No credential is stored in the repository. All configuration arrives through environment
variables documented in `.env.example`, and `.env` is gitignored and blocked by a pre-commit
hook.

## Environments and scale

Three profiles, selected by `NORDBANK_ENV`:

| Profile | Scale | Footprint | Purpose |
|---|---|---|---|
| `ci` | About 500 customers and tens of thousands of transactions | Under 100 MB of parquet and warehouse combined | A full pipeline run completes in under a minute, so CI can run end to end on every pull request |
| `dev` | About 5,000 customers and a few million transactions | About 2 GB | The default for local work: large enough for incremental logic to be meaningful, small enough to rebuild over a coffee |
| `full` | About 250,000 customers and tens of millions of transactions | About 25 GB | Exercises partition pruning, incremental models and the single-writer constraint under load |

The profiles differ only in generator parameters and dbt variables. No model, DAG or contract
is conditional on the profile; if a transformation only works at small scale, that is a
defect, not a configuration.
