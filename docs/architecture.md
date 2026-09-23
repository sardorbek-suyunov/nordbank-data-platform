# Architecture

Nordbank is a simulated EU-licensed neobank. The platform ingests a synthetic core banking
system and four external feeds into a medallion lakehouse, transforms them with dbt, and
serves a dimensional model to Power BI and a Streamlit application. Everything runs on one
machine; the cloud footprint exists only to prove the transformation layer is portable.

## Sources

| Source | Type | Load pattern | Cadence |
|---|---|---|---|
| Core banking (Postgres, synthetic) | Relational OLTP | Incremental by watermark on `updated_at`, soft deletes | Daily |
| ECB FX rates (Frankfurter API) | REST API | Interval, one request per date; unpublished dates absent in bronze, gap-filled in silver | Daily |
| Card network settlement files | CSV in object storage | File arrival, identity by content checksum; a late file lands in the partition of its arrival | Daily |
| Sanctions / PEP list (OpenSanctions schema, synthetic content) | Bulk snapshot | Full refresh, versioned by the publisher's version string | Weekly by platform choice; the publisher exports four times a day |
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

**The overlap's cost is bounded by the rows at the watermark, not by elapsed time, and for
reference data that bound is the whole book.** A run re-reads every row whose `updated_at`
lies within `EXTRACT_LAG` below the watermark. For a `core` entity that is the tail of the
previous day. For a `ref` entity it is everything: the reference seed writes each table in one
transaction, so its rows share one `updated_at`, the watermark sits on that instant, and the
watermark can never advance past a tie cluster that never changes. Reference extraction
therefore re-reads its whole book on every run, and bronze holds one copy of it per run.

Measured on specification 005's sixty-day backfill: all twenty-nine reference entities landed
their entire book on every one of sixty-two runs, and no reference watermark moved once. That
is 441 rows a run and 27,342 in total against 283,036 `core` rows, so about ten per cent of
the backfill's bronze rows are repeated reference data. The cost is constant per run rather
than growing, which is why it is accepted rather than fixed here; it is also the reason
specification 005's first reconciliation reported a 441-row discrepancy that was the control
working.

It is an M7 metric with a threshold rather than a note. `ops.source_reconciliation_daily`
already exposes `rows_re_read` per entity and day; M7 adds a `dq` check of severity `info`
recording each entity's re-read share per run, escalating to `warn` when an entity re-reads
all of what it lands on more than seven consecutive runs — a watermark that has not moved in a
week while the entity keeps landing rows. Against the measurement above it fires for all
twenty-nine reference entities, which is the intended outcome: the cost becomes a reported
number rather than a surprise. The remedy, if the number ever matters, is a cursor that
records the primary keys already registered at the watermark instant, so that a tie cluster
is read once; it is not built here because at `ci` and `dev` the book is small.

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

**ECB FX rates.** The API is queried once per date, for the run's logical date. The ECB
publishes on working days only, at around 16:00 CET, which is 15:00 UTC in summer and 14:00 UTC
in winter. Nothing in the platform assumes a fixed UTC publication hour: the freshness window
for this source is derived from the CET publication time and the daylight saving offset in
force on the day, which is why the window is stated with a grace rather than as a deadline.

**A date the ECB did not publish is absent in bronze, and the API does not say so.** Measured
against Frankfurter v1: a request for a Saturday, a TARGET holiday or a date after the latest
publication returns HTTP 200 with the previous publication's rates and that publication's date
in the `date` field; only a date beyond all data returns 404. So the landing rule is that a
response is written only when its `date` equals the date requested, and otherwise nothing is
written — not a null row and not a flagged row. That comparison is the whole control, and it
also makes a rate for a simulated day ahead of the real clock impossible to fabricate: the API
answers such a request with an earlier date, and the rule lands nothing.

Weekend and holiday gaps are filled in **silver**, by carrying the last published rate forward
into a rate table with one row per currency per calendar date, because conversion must not
fail on a Sunday transaction. Carried rows are flagged, so the fill is visible downstream.
Filling at ingest would destroy the evidence that the gap existed.

**Card network settlement files.** Files land under a bucket prefix and a deferrable sensor
releases the DAG when the file for the day is there. A file is identified by the checksum of
its content, so a file reprocessed or renamed never lands twice. A file names the settlement
date it covers, which may be earlier than the day it arrives. **A late file lands in the
ingest partition of its arrival**, with its settlement date as a business attribute: bronze
partitions by ingest date and never rewrites a registered partition (ADR 0008), so a file for
settlement date D−3 arriving today is today's batch. What is rebuilt for the affected
settlement dates is the reconciliation mart, at M6.

**Sanctions and PEP list.** The snapshot is loaded whole and stored under the publisher's own
version string, with the publisher's export timestamp recorded as its publication time. A
snapshot whose content checksum the platform has already landed is a no-op. Screening results
reference the version they were produced against, so a past decision can be explained with the
list as it stood that day.

Two cadences are involved and they are not the same fact. The real OpenSanctions consolidated
list exports four times a day, on the schedule `0 1,7,13,19 * * *`, and each export carries a
new version string; that is recorded in the contract, as the publisher's behaviour. The
platform chooses to ingest weekly, and that choice is what the freshness SLA in
`metric_definitions.md` measures.

**The list's content is synthetic, and that is a decision rather than a simplification.**
It reproduces the FollowTheMoney entity schema the real list uses, and its entities
are generated, including the fixture M3 planted so that screening has something to match. The
real list holds some three hundred thousand designated persons and entities; landing it would
put real special-category personal data into a lake that feeds a publicly deployed application
at M9, with no lawful basis and no retention story.

**Macro indicators.** Series are appended monthly. Revisions to already published periods are
expected; the load is an upsert keyed on series and period, and the previous value is kept in
the silver history.

### The simulation is not part of the platform

One DAG writes to the source database, and the distinction matters enough to state rather than
leave to be inferred from a connection id.

`ops_source_tick` advances the simulated core banking system by one business day. It is
**simulation infrastructure standing in for the source system's own operation** — the nightly
batches, the customer activity and the back-office corrections a real bank's core system would
perform for itself. A real deployment would not have it, because a real bank's core system
already runs. It exists here because the source is generated, and M4's extraction needs a
source that changes rather than a static snapshot.

It is therefore not part of the data platform, and it is the only thing in this repository that
writes to `core`. It authenticates through a connection of its own, `nordbank_source_simulator`,
as the application role, and the name is chosen so that a reader scanning the connection list
can see which side of the line it sits on.

**The extraction path remains read-only and is unchanged.** Every ingestion DAG from M4 onward
authenticates through `nordbank_source_db` as `nordbank_reader`, which holds `select` and
nothing else, and spec 002's acceptance criterion 8 proves all four refusals. Nothing in the
platform's own path acquires the ability to write to a source, and no extraction task shares a
connection with the simulator.

The DAG is paused by default, so the capability is not exercised unless somebody asks for it.

## Layer contracts

**Bronze guarantees** that what is stored is what the source sent, with direct identifiers
already replaced by tokens (see Security and PII below).

Fidelity and typing are reconciled as follows. For API and file sources, bronze stores the
record payload in a `_raw_payload` column alongside the parsed and typed columns. For the
relational source, the row as extracted is the payload. The parsed columns are a convenience;
the payload is the evidence.

The payload is structurally faithful rather than byte-identical. Its shape, field names,
nesting, ordering and every non-identifier value are exactly what the source sent; fields
classified as identifiers carry their tokens in place of the cleartext values. The consequence
is worth stating plainly: bronze is not a byte-for-byte copy of the source, and reconstructing
an original record requires the vault. A payload that kept cleartext identifiers would put
personal data outside the vault, where erasure cannot reach it, which would defeat
crypto-shredding entirely (ADR 0005).

Typing is applied against the contract in `contracts/`, and a value that fails its cast is
never coerced to null and never silently dropped: the row is written to the quarantine table
for that entity with the name of the failing column, the failure reason, and the offending
value.

For a column classified as an identifier in [pii_classification.md](pii_classification.md),
the quarantine row stores the token, not the cleartext value. Quarantine is a mutable table
that sits beside bronze rather than inside it, so a cleartext identifier written there would
be personal data in a place the vault does not cover. Quarantine tables are inside the scope
of the erasure workflow and are treated as warehouse data, not as a debugging scratch area.

The source row count is therefore always reconstructable as landed rows plus quarantined rows,
for every batch.

**What the ingest gate is for, and what it is not for.** The gate rejects records that cannot
be trusted as records: a missing or duplicated primary key, a value that cannot be parsed as
its declared type, a structurally malformed record. Expectations about field *content* are
data quality concerns, measured and reported downstream in `dq`, and never ingest gates.

The reason is that the two failure modes are not symmetric. A record rejected for being
malformed is a record nothing could have used. A record rejected for a soft expectation — a
customer whose email address the source has cleared, say — is a usable record removed from the
population, and because the condition usually persists, the same record is removed on every
subsequent day it changes. The entity stops reaching bronze, silver carries a history that
truncates mid-stream, and every count built on it is quietly low. Rejecting a whole record for
a soft expectation trades a visible quality signal for invisible population loss, which is a
data-loss defect in the costume of a control. The expectation belongs in `dq`, where an
uncontactable customer is a measured share rather than a deleted row.

**No partial load is a guarantee at entity grain, not at run grain.** One entity failing its
contract or hitting breaking drift leaves that entity's batch quarantined, marked `failed` and
its watermark unmoved; the entities that extracted cleanly in the same run are still
registered. The alternative — refusing to register anything when any entity fails — would let
one entity's drift stop every other entity's pipeline from that day forward, and the watermarks
would not advance for any of them. The run still fails, through a terminal gate task, so the
failure is not swallowed; what does not happen is fifteen healthy entities being held hostage
by the sixteenth.

Beyond typing, bronze applies nothing: no renaming past casing, no joins, no business rules,
no deduplication except on the exact replay of a batch. Bronze is append-only and immutable
once a partition is registered; a correction is a new batch, never an edit. Every row carries
`_ingested_at`, `_source_file`, `_batch_id` and `_source_system`.

Because a failed run can leave objects behind that no batch registration ever covered, every
bronze model filters to batch ids registered as successful in `ops`. Reading the object store
without that filter is a defect: the files are there, they look complete, and nothing about
them says the run that wrote them died.

*Schema drift.* An additive change is accepted: a new source column is noticed, its appearance
is recorded in `meta` with the batch that introduced it, and it is **not** written to bronze,
because bronze carries what the contract describes and an unknown column is noticed rather
than silently absorbed. A type change, a removed column, or a change to the primary key is not
accepted: that entity's whole batch is quarantined and the ingestion gate fails, so a partially
conforming load never reaches bronze. Resolving the drift means a new contract version and an
explicit rerun.

One consequence of the additive case reaches silver and is recorded here rather than
discovered there. A source row can be updated in a way that changes only the column the
contract omits, and bronze then receives a version in which every contract-described column
equals its predecessor's. Silver must expect a version with no visible differences and must
not treat it as a defect or deduplicate it away. The generator produces exactly this after its
scripted additive event: `core.merchants` rows are revised only in `merchant_risk_score`.

**The contract and the data dictionary record two different facts, and neither is a copy of
the other.** `docs/data_dictionary.md` records what the source *is*, and `make schema-check`
proves the live schema matches it. A contract under `contracts/` records what the platform has
*agreed to accept*. They are bootstrapped equal, once, and the lag between them afterwards is
the mechanism by which drift becomes visible at all: a contract regenerated from the dictionary
on every run would describe the source perfectly and detect nothing.

This is not one fact maintained in two places, and the standing rule in `docs/specs/README.md`
is not being bent. It must not be described as a duplication either, because a reader who
believes it is will delete a copy and remove the control. `make contracts-diff` reports how far
each contract has drifted from the dictionary and names the dictionary revision it was pinned
to, so "the dictionary moved" and "the contract was edited" are distinguishable; `CHECK=1`
fails on any divergence and runs in CI.

**Silver guarantees** one row per business entity per version. It applies deduplication on the
business key, resolves late arrivals by `updated_at`, applies soft deletes, converts amounts
to `DECIMAL(18,4)` with an EUR equivalent, and normalises timestamps to UTC.

*Business date, posting date and audit time are three different dates on a late-arriving item,
and silver has to keep them apart.* An offline card transaction presented days after the
customer made it carries `booked_at` on the day of the tap, `updated_at` on the day it reached
the bank, and a ledger entry whose `posting_date` is that later day. The ledger posts to the
current open period rather than to the business date, for the same reason FX rates do not
restate: posting into a closed period would change totals for a day that has already been
reported, and yesterday's figure would stop being reproducible from yesterday's data. Silver
orders by business time and the ledger by posting date, and the two are not interchangeable.

A soft delete and a deactivated reference code are not the same thing and are not applied the
same way. `is_deleted` on a `core` row means the entity is gone, and silver removes it.
`is_active = false` on a `ref` row means the code is no longer offered, and silver **retains**
the row: a dimension must still describe historical facts that reference a retired code, and a
transaction booked under a channel the bank has since withdrawn still needs that channel to
have a name. `ref` tables therefore carry `is_active` and never `is_deleted`, which is what
makes the two cases impossible to confuse (spec 002 design rule 3).

Identifiers stay tokenised: silver never resolves a token, and a keyed hash could not be
resolved without the vault in any case. The attributes reporting needs are derived instead by
generalising quasi-identifiers, which are retained in the clear precisely so that they can be:
an age band from the date of birth, a country and region from the address, a tenure band from
the signup date. Which columns are identifiers and which are quasi-identifiers is defined in
[pii_classification.md](pii_classification.md). Entities that mutate are SCD2 with `_valid_from`,
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

When no rate exists at all for a currency with `rate_date <= transaction_date`, the conversion
has no answer and does not invent one. `amount_eur` is null, `fx_is_missing` is set on the
row, and a `dq` check of severity `error` fires for that currency and date. The converted
amount is never zero and never silently the original amount in another currency: both are
wrong numbers that add cleanly into a total and are invisible afterwards, whereas a null
propagates visibly and an error blocks the gate.

## Storage layout

Raw extracts land in MinIO under the bucket `nordbank-lake`:

```
bronze/<source>/<entity>/ingest_date=YYYY-MM-DD/batch_id=<batch_id>/part-NNNN.parquet
```

Partitioning is by ingest date rather than business date, so a partition is written once and
never revisited. Business date filtering happens in silver, where late arrivals can be
ordered correctly.

The batch id in the key is what makes bronze immutable. Two writes cannot collide on a key,
because the batch id differs, so an overwrite is not something the platform declines to do but
something it cannot express (ADR 0008). The cost is more and smaller objects, which needs a
compaction and retention story before the `full` profile is usable, and a rule that every
bronze model filters to batches registered in `ops`, or it will read the output of runs that
failed after writing.

The warehouse is a single DuckDB file holding the `bronze`, `silver`, `gold`, `dq`, `ops` and
`meta` schemas. Bronze tables read from the parquet files; from silver upward the data is
materialised in DuckDB.

## Orchestration

Airflow 3.x with asset-driven scheduling. Ingestion DAGs produce assets named for the entity
they land. Transformation DAGs consume those assets, so silver runs when its inputs are
present rather than on a timer that hopes they are. Quality gates run after the layer they
check and publish their results as assets in turn, which lets a mart wait on a passing gate.

DuckDB allows one process to hold the warehouse file, and a writer excludes readers as well as
other writers. Every task that touches the warehouse, reading or writing, therefore acquires
the Airflow pool `warehouse_access`, which has one slot. Access from outside Airflow goes
through a helper that retries with bounded backoff, since the pool governs only what Airflow
schedules. The measured behaviour and its cost are recorded in ADR 0002.

Pipeline state lives in the `ops` schema: a batch registry with one row per extraction batch,
per-entity watermarks, run outcomes, and freshness measurements per source.

Rerun semantics are precise about what immutability means. A task retry inside a batch that
has not yet been registered overwrites its own partial output: nothing downstream can see an
unregistered partition, so replacing it rewrites nothing that anyone has read. Once the batch
is registered, that partition is final. A rerun after registration extracts again under a new
batch id and writes a new partition; the earlier partition is not modified, and silver
deduplicates the overlap on the business key and `updated_at`.

Bronze immutability is therefore a property of registered partitions, not of every file that
has ever appeared in the bucket.

## Consumption

Neither consumer reads the live warehouse file. Both read a published snapshot: the gold
models are exported to Parquet under `exports/` by a dedicated task at the end of a
transformation run, and that snapshot is what reports and applications open.

This is a consequence of the engine, not a preference. A DuckDB file can be held by one
process at a time, so a report or an application holding the live file would block every
transformation task for as long as it stayed connected, and would itself be blocked whenever a
task held the file. A snapshot has no such contention, and it is also the only arrangement
that works once the Streamlit application is deployed remotely, where the warehouse file is
not reachable at all.

Power BI holds the semantic model: relationships, DAX measures, and the executive reports. It
reads the exported Parquet through a folder source. From M10, when the BigQuery target exists,
Power BI connects to BigQuery natively and the export path becomes the local route rather than
the primary one.

The Streamlit application is the publicly deployable face of the project and the operational
view: pipeline state, freshness, quality results, and a small number of analytical screens. It
reads the same exported snapshot, and never triggers a pipeline run.

The snapshot is therefore a published artifact with its own freshness: a report shows the
state as of the last export, and the export timestamp is displayed alongside the numbers so
that nobody reads a stale figure as a live one.

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

Quarantine follows the same rule as bronze. A value that fails its cast in a column
classified as an identifier is quarantined as its token, and quarantine tables are inside the
scope of the erasure workflow. A mutable side table full of cleartext identifiers would be the
obvious hole in this design, so it is closed explicitly rather than left to discipline.

Which columns are identifiers, which are quasi-identifiers, which are sensitive and which are
not personal at all is defined in [pii_classification.md](pii_classification.md). Every column
in every contract carries a classification from M2 onward, and the classification is what the
tokenisation, generalisation and erasure rules dispatch on.

The token key is supplied by `PII_TOKEN_SALT` and is never committed. Gold exposes tokens and
generalised attributes such as country and age band, never cleartext identifiers.

The erasure workflow itself is a governance DAG (`gov_erasure`), implemented at M8, which
records each erasure in `meta` with its request, timestamp and the tokens affected, so the
platform can prove what it did without retaining what it erased.

### Sanctions screening reads the vault

A payment's counterparty name is an identifier, so it is tokenised at ingest like any other,
which appears to make question 12 impossible: a sanctions list is matched on names, and silver
holds a hash.

Screening therefore runs as a **governance-domain job with vault access**, not as a
transformation in silver. It resolves tokens to names through the vault, screens them against a
named list version, and persists only the token, the matched entity, the list version and the
match score. The raw name never leaves the vault, and never reaches silver, gold or an export.

Screening at ingest time, before tokenisation, was the obvious alternative and is wrong. A
sanctions list changes weekly, and the question a compliance function actually asks is whether
anyone the bank has already paid appears on the list as it stands today. Screening once at
ingest answers that only for the list as it stood then, and re-screening the book against an
updated list is how the control is really operated. Resolving through the vault at screening
time is what makes historical re-screening possible at all.

The consequence follows and is stated rather than discovered: **erasing a subject also destroys
the ability to re-screen them.** Their tokens no longer resolve, so no future list version can
be matched against them. That is correct — the subject has exercised a right to be forgotten,
and what remains is a hash with no surviving key material — but it is a real capability lost at
a real moment, and it belongs in the erasure record rather than in a surprise.

This makes the vault load-bearing for a second reason. It was already the only path to erasure;
it is now also the only path to a sanctions screen. The concentration of risk noted in ADR 0005
is correspondingly larger, and access to `meta` is correspondingly more restricted than access
to the warehouse.

The rationale, the rejected alternatives and the full set of consequences are in ADR 0005.

No credential is stored in the repository. All configuration arrives through environment
variables documented in `.env.example`, and `.env` is gitignored and blocked by a pre-commit
hook.

## Environments and scale

Three profiles, selected by `NORDBANK_ENV`:

| Profile | Scale | Lake and warehouse footprint | Purpose |
|---|---|---|---|
| `ci` | About 500 customers and tens of thousands of transactions | Under 100 MB of parquet and warehouse combined | A full pipeline run completes in under a minute, so CI can run end to end on every pull request |
| `dev` | About 5,000 customers and a few million transactions | About 2 GB | The default for local work: large enough for incremental logic to be meaningful, small enough to rebuild over a coffee |
| `full` | About 30,000 customers and tens of millions of transactions | About 25 GB | Exercises partition pruning, incremental models and the single-writer constraint under load |

The profiles differ only in generator parameters and dbt variables. No model, DAG or contract
is conditional on the profile; if a transformation only works at small scale, that is a
defect, not a configuration.

### The source database, measured

The footprint column above is about the lake and the warehouse, which M2 does not build. The
source database is a separate thing and these are the figures the M2 generator actually
produced on a freshly nuked and schema-applied stack, on the machine in
`project_state.md`. Re-measured at M3, after four defects at the boundary of the history window
were fixed: `ci` is 22 per cent smaller and `dev` 6 per cent, and every row of the difference was
an artefact one of those defects produced. The two profiles differ by so much because the worst
of them scaled with how short the history was — a second account opens 14 to 900 days after
signup, and over six months almost all of those dates fall past the anchor while over three years
most do not.

`dev` stays inside spec 003's band of two to four million transactions and inside its six-minute
target, and it loads faster than it did despite four indexes the mutation engine's read path
needed.

| Profile | Customers | History | Transactions | Total `core` rows | Source database | Generate and load |
|---|---|---|---|---|---|---|
| `ci` | 500 | 6 months | 31,157 | 163,281 | 54 MB | 11.2 s |
| `dev` | 5,000 | 3 years | 2,166,281 | 10,770,708 | 2,641 MB | 274.6 s |
| `full` | 30,000 | 5 years | about 23 million, projected | about 115 million, projected | about 28 GB, projected | not run |

**The `full` profile's customer count was set by this measurement.** The specification
originally set it at 250,000 customers, which at the per-customer intensity the `dev` profile
validates projects to roughly 190 million transactions and 900 million rows — hundreds of
millions rather than the tens of millions the specification targets.

What moved was the customer count, not the intensity. Per-customer intensity is a validated
realism parameter, justified line by line in `generator_realism.md`; lowering it to hit a row
count would make the `full` profile a less realistic bank than `dev`, which is the opposite of
what a larger profile is for. `full` is therefore 30,000 customers, which lands on the stated
target. It exists to prove that incremental extraction beats full refresh and to exercise the
single-writer constraint under load, and 23 million transactions demonstrates both as well as
190 million would.

The `full` row is arithmetic on the `dev` measurement and is not itself a measurement. **That
profile has not been run**, and measuring it is an outstanding follow-up in `project_state.md`.

The `dev` target moved from five minutes to six for the same reason: it is set by measurement
now rather than by an estimate made before anything was built. Two optimisation passes took it
from about 23 minutes to 5.5, and the residual is generation and `COPY` rather than overhead.
Spec 003 carries both as amendments.

The `full` profile cannot be seeded row by row. At tens of millions of rows, a generator that
issues one INSERT per record turns the historical load into hours of work and makes the
profile unusable in practice. The M2 historical load writes through `COPY` from generated CSV
or Parquet batches, and the M3 mutation engine batches its writes the same way. This is a
constraint on the generator design rather than a runtime switch, which is why it is recorded
here and not in a configuration file.
