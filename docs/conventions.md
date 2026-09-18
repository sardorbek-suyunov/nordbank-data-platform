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
| Lake | MinIO bucket `nordbank-lake`; `bronze/<source>/<entity>/ingest_date=YYYY-MM-DD/batch_id=<batch_id>/part-NNNN.parquet`; the batch id in the key is what makes an overwrite impossible (ADR 0008) |
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
- `meta` holds contract versions, column lineage, the model catalogue and the PII vault.

## Naming

**Bronze.** `br_<source>__<entity>`, with a double underscore between source and entity so the
source stays readable when the entity name itself contains an underscore, for example
`br_corebank__loan_installments`.

**Silver.** `sl_<entity>`, where `<entity>` is plural and mirrors the source entity name
exactly: `sl_customers`, `sl_loan_installments`, `sl_gl_entries`. No renaming, no
singularisation, no interpretation. The rule is mechanical so that two engineers deriving a
name from the same source table arrive at the same answer.

**Gold.** Dimensions are singular: `dim_customer`, `dim_merchant`, `dim_date`. Facts are named
for their grain, not for their source: `fct_transactions` is one row per transaction,
`fct_account_balance_daily` is one row per account per day. If the grain cannot be read off
the model name, the name is wrong. Marts are `mart_<domain>_<subject>`.

A many-to-many relationship between two dimensions is a bridge, named
`bridge_<relationship>` and singular: `bridge_account_holder` is one row per account and
holder. A bridge carries the allocation factor the relationship needs, so that monetary
measures can be split while counts are not.

**Columns.** `snake_case`. Business keys keep the source name and the `_id` suffix. Surrogate
keys take the `_sk` suffix and are the hash of the business key, plus the valid-from
timestamp where the entity is SCD2. Booleans read as a statement: `is_active`,
`has_collateral`. Dates end in `_date`, timestamps in `_at`. Money columns end in the
currency treatment they carry: `_amount` is in the transaction currency and travels with a
`_currency_code` column, `_amount_eur` is the converted value. Platform-generated columns
start with an underscore, so a column beginning with `_` is never sourced from the bank.

**`created_at` and `updated_at` are reserved.** No business timestamp on any table may take
either name. Both are audit columns whose meaning must be uniform, because watermark
extraction depends on it: a table where `created_at` held a business event time would have
that column written by the loader and its `updated_at` written by the trigger, so the two
would disagree and `created_at` would no longer say when the row entered the database. A
business timestamp is named for its event: `alerted_at`, `booked_at`, `started_at`.

**DAG ids.** `ingest_<source>` for extraction into bronze, `transform_<layer>` for dbt runs,
`dq_<scope>` for quality gates, `ops_<purpose>` for maintenance, `gov_<purpose>` for
governance work such as erasure. The DAG id, the module filename and the Airflow asset prefix
are identical.

## Numeric types

| Kind of value | Type | Notes |
|---|---|---|
| Monetary amounts | `DECIMAL(18,4)` | Floating point is prohibited, including in intermediate arithmetic |
| Ledger amounts | `DECIMAL(18,4)`, **signed** | Documented exception to the non-negative guidance, below |
| Exchange rates and interest rates | `DECIMAL(18,8)` | Four decimals is not enough for a rate that multiplies a balance |
| Percentages and ratios | `DECIMAL(18,8)` as a decimal fraction | 0.0425 means 4.25 per cent. Never stored as 0 to 100, and never as an integer |

Formatting a fraction as a percentage is the presentation layer's job. A column holding 4.25
for 4.25 per cent is a defect, because the next person to multiply by it will be wrong by two
orders of magnitude and nothing will fail.

**Signed ledger amounts are a deliberate exception.** `core.gl_entries.amount` is signed: a
debit is positive and a credit is negative, bound to `entry_side_code` by a check constraint.
The general guidance that amounts are non-negative where the domain requires it does not apply
to it. This is recorded here because the alternative looks like a defect to anyone who has not
read spec 002: with signed amounts, "the batch balances" is literally `sum(amount) = 0` per
currency, and making the column non-negative would replace one check with a comparison of two
aggregates and gain nothing. Do not "fix" it.

## Reference data and categorical values

**A categorical value earns a lookup table in `ref` if either limb holds.**

*Behavioural attribute.* The table carries at least one attribute beyond `code`, `name` and
`is_active` that a model or metric named in `metric_definitions.md` or `model_inventory.md`
consumes, and that attribute adds information the code does not already carry.
`transaction_types.is_customer_initiated` is the template: it groups eight codes into one flag.
A boolean that is true for exactly one code of a two-value domain restates the code and does
not qualify.

*Open vocabulary.* The set of values is open and expected to grow with data rather than with
schema changes. A vocabulary that grows with data must live in data, or every new value is a
schema migration in a system whose purpose is generating varied data. This limb is independent
of the first and is the stronger of the two, because it is about where a value can be added
from rather than about what reads it. `loan_applications.decision_reason_code` qualifies on it
alone and carries no attribute at all.

A categorical domain satisfying neither limb is a **check constraint against a value list
documented in `data_dictionary.md`**, on the column that holds it, not a table.

**The stopping rule.** A categorical attribute of a `ref` row stays a plain column unless it is
a join key shared with another table. Without it the rule recurses into a lookup for every
adjective on a lookup.

**`is_deleted` in `core`, `is_active` in `ref`, and the difference matters.** A `core` table
carries `created_at`, `updated_at` and `is_deleted`. A `ref` table carries `created_at`,
`updated_at` and `is_active`, and never `is_deleted`: reference rows are deactivated, not
deleted, and two overlapping flags on one row would be a defect rather than thoroughness. The
distinction is a downstream contract, not a naming preference. `is_deleted` on a `core` row
means the entity is removed from silver. `is_active = false` on a `ref` row means the row is
**retained**, because a dimension must still describe historical facts that reference a retired
code: a transaction booked under a channel the bank has since withdrawn still needs that
channel to have a name.

## Currency provenance

Wherever an `_amount_eur` column exists, `fx_rate` and `fx_rate_date` exist beside it, and
`fx_is_carried` where the rate may have been carried forward. A converted amount with no
visible rate provenance is a defect: the number cannot be reproduced, checked or explained
without them.

`fx_is_missing` marks a row for which no rate existed at or before the transaction date. Such
a row has a null `amount_eur` and raises a `dq` check of severity `error`. A missing rate is
never written as zero and never as the unconverted amount.

## Dimensional modelling

**Reserved members.** Every dimension carries two reserved rows: surrogate key `-1` for
Unknown, meaning the business key was absent or did not resolve, and `-2` for Not applicable,
meaning the relationship does not exist for that fact.

**Joins.** Facts join dimensions with a left join and coalesce the resulting surrogate key to
`-1` or `-2`. A fact row is never dropped because a dimension record is missing, and a
missing dimension record never silently becomes a null that disappears from a grouped result.
A rising count of `-1` members is a data quality signal, and it is reported as one in `dq`.

**SCD2 resolution.** A fact resolves its dimension surrogate key as of the event timestamp:

```
on  f.event_at >= d._valid_from
and f.event_at <  d._valid_to
```

so that a transaction from March joins the customer as they were in March, not as they are
today.

Every SCD2 dimension also exposes the natural business key as a durable key, stable across
versions. Current-state slicing and the Power BI relationships that point at latest state use
the durable key; history-aware facts use the surrogate key. Both columns are present on every
SCD2 dimension, and neither is optional.

## Data classification

Every column in every contract carries one of four classifications: `identifier`,
`quasi-identifier`, `sensitive` or `non-personal`. The classification decides whether the
column is tokenised at extraction, generalised before gold, access-restricted, or left alone.
The taxonomy and the handling rule for each class are in
[pii_classification.md](pii_classification.md).

Two rules follow from it and are stated here because they are naming and modelling rules
rather than privacy narrative. A tokenised column keeps its source name and the `_id` suffix
where the source had one, so `customer_id` holds a token and not a cleartext value; nothing is
renamed to advertise that it was tokenised. A generalised column is named for the
generalisation, not for the source column it came from: `age_band`, not `date_of_birth_band`.

## Tests

A generic dbt test is declared in the model schema file next to the column it constrains. A
singular test lives in `dbt/tests/` and is named for the rule it enforces, for example
`assert_gl_entries_balance_by_day.sql`.

Every model declares its primary key and tests it with `unique` and `not_null`. A model that
cannot is exempt only by documenting the exemption in its schema file, with the reason. An
undeclared grain is not an exemption, it is an unfinished model.

Every test and every quality check declares a severity, once, where it is defined:

| Severity | Meaning | Effect |
|---|---|---|
| `error` | The data is wrong in a way that makes downstream numbers wrong | Fails the gate, blocks promotion of that layer, pages the owner |
| `warn` | The data is suspicious but usable | Recorded in `dq`, does not block, reviewed in the trend |
| `info` | A measurement kept for trend only, such as row count drift or the share of Unknown dimension members | Recorded in `dq`, never blocks |

The same three levels are used by `docs/metric_definitions.md` when it defines the quality
check pass rate, so a check has one severity everywhere it appears.

## Commits

Conventional Commits, imperative mood, lower case subject, no trailing period, one logical
change per commit. Types in use: `feat`, `fix`, `docs`, `build`, `ci`, `refactor`, `test`,
`chore`. The number of commits in a change is whatever coherent, independently reviewable
units produce; there is no target count.

No emoji anywhere in the repository, including commit messages.

Commit messages and pull request descriptions carry no attribution trailers: no
`Co-Authored-By` lines and no generated-with notices. This applies to every contributor and
every tool.

## Contributing

From M2 onward, work happens on a branch and lands through a pull request. `main` is never
written to directly.

- Branch names are `feat/M<n>-<slug>`, for example `feat/M2-source-ddl`.
- A pull request is required, and four status checks must pass before it can merge: `lint`,
  `dags` and `docs` from the `ci` workflow, and `stack` from the `stack` workflow.
- **Rebase merge only.** Squash merging and merge commits are disabled in the repository
  settings. The commits in a branch are written to be individually reviewable, and squashing
  them into one would throw that away; a merge commit would add a node that says nothing.
- `main` is protected: all four status checks required, a pull request required, force pushes
  refused, and `enforce_admins` on.

A pull request description is an engineering summary, under the same prose rules as the
documentation: what changed, why it changed, how it was verified, and what was deliberately
left out. "Updates" is not a description. If the branch closes a milestone, link the
specification and the checkpoint.

A milestone is not closed until `docs/project_state.md` reflects it. Updating that document is
part of writing the checkpoint, not a follow-up to it.
