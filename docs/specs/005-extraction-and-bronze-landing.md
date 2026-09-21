# 005 — Extraction and Bronze Landing

Status: Approved
Version: 2
Supersedes: version 1, 2026-09-21, unimplemented
Depends on: 000, 001, 002, 003, 004

## Goal
Extract the core banking source incrementally by watermark, validate against a
contract, tokenise identifiers, land immutable Parquet in the lake, and record
every batch in the warehouse's operational schema. Both source schemas are in
scope: the sixteen `core` entities and the twenty-nine `ref` entities. This is
the first half of M4 and it establishes the framework the external feeds reuse.

## Scope

### 1. Warehouse operational schema
Numbered idempotent SQL under `infra/warehouse/schema/`, applied by
`make warehouse-apply`. These are platform state written by Python, not derived
models, so they are not dbt's concern. The warehouse file lives on a named
volume and is not reachable from the host, so the target runs inside a
container, as `make health` and `make test-integration` already do.

- `ops.extract_watermark` — one row per source system and entity: the highest
  `updated_at` successfully registered.
- `ops.batch_registry` — one row per batch: batch id, source system, source
  schema, entity, ingest date, data interval, watermark from and to, rows
  landed, rows quarantined, object prefix, status, timings, and the triggering
  run id. Status is `open`, `written`, `registered` or `failed`. **Only
  `registered` batches are readable downstream.** The batch's open timestamp is
  a column of this table and is the value every record's `_ingested_at` carries,
  so a retry reads it back rather than taking a new clock reading (section 5).
  The triggering run id is provenance and is never a key: section 4 says why.
- `ops.source_reconciliation` — per entity per source day: counts claimed by
  the source, landed, quarantined, and the difference. Landed here is scoped by
  the row's own `updated_at` date, not by the batch, because a batch spans the
  overlap window and a source day does not. The registry's `rows_landed` and
  this table's landed count are two different numbers and neither substitutes
  for the other.
- `meta.pii_vault` — token to raw value, with classification, source entity and
  column, and the batch that first saw it. **The primary key is the token.**
  Entity and column record where the value was first sighted and are not part of
  the key, because one raw value legitimately occurs in more than one entity.
- `meta.contract_version` — the contract version in force per entity, and when.
- `meta.schema_drift_log` — drift observed at ingest: entity, column, kind,
  batch, action taken.
- `dq.quarantine_log` — one row per rejected record: batch, entity, column,
  reason, and the offending value **tokenised if the column is an identifier**.
  This table is a rebuildable index. The durable evidence is the quarantine
  Parquet in the lake (section 5), and this table can be reconstructed from it.

### 2. Contracts
`contracts/corebank/<entity>.yml`: contract version, the source schema, the
watermark column, the primary key, the dictionary revision the contract was
bootstrapped from, and per column its name, type, nullability and
classification. Entity names are unique across `core` and `ref`, so the
directory stays flat; the bootstrap refuses to write two contracts with the same
entity name rather than leaving the collision to be discovered in the lake.

**The dictionary and the contract record two different facts.** The dictionary
records what the source *is*, and `make schema-check` proves the live schema
matches it. The contract records what the platform has *agreed to accept*. They
start equal and the lag between them is the mechanism by which drift becomes
visible at all. This is not one fact maintained in two places, and it must not be
described as one: a reader who believes it is will delete a copy and remove the
control.

- `make contracts-bootstrap` generates a contract from the dictionary for any
  entity that has none, recording the dictionary revision it was generated from.
  A one-time bootstrap, not a sync.
- Thereafter contracts are hand-authored and version-bumped deliberately.
- `make contracts-diff` reports divergence between each contract and the current
  dictionary, and names the dictionary revision each contract was pinned to, so
  "the dictionary moved" and "the contract was edited" are distinguishable.
- `make contracts-diff CHECK=1` exits non-zero on **structural divergence** and
  runs in the existing `docs` CI job. It does not become a fifth required check.

**Structural divergence and declared strictness are different findings.**

- *Structural divergence* is a type change, a column present in one and absent
  from the other, or a different primary key. It is a finding and `CHECK=1`
  fails on it.
- *Declared strictness* is the contract narrowing what the source permits: a
  column the source declares nullable that the contract declares non-nullable.
  It is intentional, it carries a stated reason in the contract file, and
  `CHECK=1` passes it. A contract is an agreement about what the platform will
  accept, not a mirror of the source, so it is allowed to expect more.

**One column is declared strict in this specification.**
`core.customers.email` is nullable in the source and non-nullable in the
contract, with the reason recorded in the contract file: a customer the bank
cannot contact is a record the platform declines to accept, and the source's
back-office correction that removes a bounced email and never replaces it
(`generator/mutation/phases/dirt.py`) is exactly the condition the expectation
exists to catch. Section 7 states what it costs.

`platform.column_classifications` is **read** by the extractor to decide what to
tokenise. It is not landed and has no contract. Nothing in the `platform` schema
is extracted: it is the simulation's own bookkeeping, not the bank's data.

### 3. Extraction, in four phases
The warehouse is single-writer and all access serialises through the
`warehouse_access` pool, so extraction must not hold the pool while reading
Postgres in bulk or writing to the lake.

1. **Open** — one pooled task: read every entity's watermark, allocate a batch
   id per entity, insert `open` rows into the registry with their open
   timestamp. One transaction.
2. **Extract** — one mapped task per entity, no warehouse access: read from
   Postgres as `nordbank_reader` where
   `updated_at >= watermark - EXTRACT_LAG`, ordered by the primary key,
   validate against the contract, tokenise identifier columns, write Parquet to
   the lake, write quarantine Parquet to the lake. Reports its counts, the
   window's lower bound and its observed maximum `updated_at`.
3. **Register** — one pooled task, trigger rule `all_done`: in a single
   transaction, mark the batches that reported `written` as `registered`,
   advance their watermarks to the observed maximum, upsert the vault, load the
   quarantine index, write the reconciliation rows, and emit the per-entity
   assets. **The watermark advances here and nowhere else.** A failure leaves
   every batch `written` and every watermark unmoved, so the next run repeats
   rather than skips. A batch whose extract task failed is marked `failed` with
   its reason and its watermark is not touched.
4. **Gate** — one terminal task: fail the DAG run if any batch of the run is
   `failed`, naming the entity and the reason.

**Why `all_done` and a separate gate.** Under `all_success` a single entity's
breaking drift would stop the register step from running at all, so the fifteen
entities that wrote cleanly would never be registered and their watermarks would
never advance — one entity's drift would kill the whole pipeline from that day
forward. **"No partial load" is a guarantee at entity grain, not at run grain.**
That distinction is the non-obvious part of the bronze contract and
`architecture.md` states it too.

**The vault write is a deliberate exception to the split, and it is bounded.**
Register re-reads from Postgres, selecting **only the identifier columns and the
primary key**, bounded by both the batch's recorded `watermark_from` and its
recorded `watermark_to`, so the re-read is deterministic and cannot pick up a
row that changed after the extract task ran. The alternatives were both worse:
carrying token-to-raw pairs through XCom would put cleartext identifiers in the
Airflow metadata database, and spilling them to a staging object would put them
in the lake, which is the one place that must never hold a cleartext identifier
(ADR 0005). Cleartext therefore travels source → register process → vault and
reaches nothing else.

**Asset emission is not transactional with the commit.** Airflow emits a task's
outlets when the task succeeds, which is after the DuckDB transaction has
committed. A crash in between leaves batches registered with no asset event.
Nothing is lost, and nothing reconciles it inside this milestone by design: the
registry is authoritative and every bronze model filters on it, so a missed
asset delays a downstream run by one interval rather than losing data, and the
next run's register emits its own assets. Detecting the gap belongs with the
operational observability at M7.

### 4. Batch identity and re-runs
Batch id is `{entity}-{interval_start:%Y%m%dT%H%M%S}-{seq:02d}`.

The sequence is determined at open time from the registry alone:

- If a batch exists for this entity and interval in status `open` or `written`,
  **reuse it**.
- Else, if the highest-sequence batch for this entity and interval is
  `registered` or `failed`, allocate the next sequence.
- Else allocate `01`.

**The rule refers to neither `run_id` nor `try_number`, and that is measured
rather than stylistic.** In Airflow 3.3.2 a retry increments `try_number` and
changes the task instance's UUID; a cleared task does the same; and a backfill
re-run of an already-completed logical date *reuses the same `run_id`* and
clears its task instances, so it is indistinguishable from a retry by run
identity. The registry's own status is the only discriminator that survives all
three. The triggering run id is recorded as provenance.

The behaviour this produces is the behaviour version 1 specified:

- A task retry or a cleared task within an unregistered batch reuses the same
  batch id and overwrites its own objects. Idempotent.
- A re-run of an interval whose batch is already `registered` allocates the next
  sequence, so the registered partition is never modified. Silver deduplicates
  the overlap.
- The register step refuses to transition a batch that is already `registered`,
  so immutability is enforced rather than assumed.

**`interval_start` is the logical date at midnight.** Airflow 3's cron timetable
has no data interval — start, end and `run_after` are the same instant — and a
manual trigger's interval is the wall clock at trigger rather than the logical
date, which is why section 10 drives this DAG by explicit runs only. The six
trailing zeros in the format are therefore constant for this source. The format
is kept as issued so the file feeds, whose intervals are not daily, reuse it
unchanged.

### 5. Object layout

```
bronze/corebank/<entity>/ingest_date=YYYY-MM-DD/batch_id=<batch_id>/part-NNNN.parquet
quarantine/corebank/<entity>/ingest_date=YYYY-MM-DD/batch_id=<batch_id>/part-NNNN.parquet
```

`ingest_date` is the date the batch ran, never the business date of the rows.
Late-arriving rows land in the partition of their arrival; ordering by business
time is silver's job.

Every record carries `_ingested_at`, `_source_file` or its source equivalent,
`_batch_id` and `_source_system`.

**`_ingested_at` is a property of the batch, not of the record.** It is the
batch's open timestamp, read back from the registry, so a retry reproduces it.
Taking a fresh clock reading per record would make a retry's objects differ from
the objects it replaces, which contradicts the byte-identity criterion 5 asks
for, and it would also be untrue: every row of a batch was ingested by one
batch, at one time.

**`_source_file` carries the qualified source relation for a relational source**
— `core.accounts`, `ref.currencies`. The column has no literal meaning here, and
it keeps its name and its position anyway, because one audit column set across
every feed is worth more than literal accuracy in one of them. The convention is
documented rather than inferred.

**The extract query orders by the primary key.** Criterion 5 asks for
byte-identical objects on retry and Postgres does not guarantee a row order
without one, so an unordered read would make the bytes differ for a reason that
is not a defect.

Written with PyArrow directly to the lake. Do not route Parquet writes through
DuckDB; it would pull the pool into the extract phase for no benefit.

### 6. Identifier tokenisation
Performed in the extract task, before anything is written anywhere.

- Token is an HMAC-SHA256 of the raw value under `PII_TOKEN_SALT`, rendered as a
  fixed-width string. Deterministic, so the same raw value yields the same token
  in every entity, column and batch, and joins survive.
- Which columns are tokenised is read from `platform.column_classifications`,
  not hard-coded. `identifier` is tokenised; `pseudonymous_key` is never
  tokenised; `quasi-identifier` is retained raw in bronze and generalised in
  silver; `sensitive` is retained and access-controlled. No `ref` column carries
  any classification but `non-personal`, so the reference DAG tokenises nothing.
- The vault records token to raw value with the batch that first saw it, keyed
  on the token. Insert-if-absent, so re-extraction does not duplicate and a
  value occurring in a second entity does not create a second row. Written by
  the register step, under the bound in section 3.
- **The vault stores the raw value, and the mapping is the key material.**
  Erasure deletes the row, which is what makes the surviving tokens
  unresolvable. Do not add a second encryption layer over the raw value.

  The reason is narrower than version 1 stated, and the difference is measured.
  DuckDB 1.5.5 supports transparent file encryption through
  `ATTACH … (ENCRYPTION_KEY …)`, with the key supplied from the environment
  exactly as `PII_TOKEN_SALT` already is, so it is not true that the ciphertext
  and its key would share a database. What is true is that the runtime holds the
  warehouse file and the environment together, so a second layer inside the same
  file defends only against **offline exfiltration of the warehouse file**; the
  mitigation that addresses the threat is a separate store with separate
  credentials, not a second layer; and application-level or file-level
  encryption of the vault adds an irreversible-loss failure mode for a fraction
  of that benefit.

  Record in ADR 0005 as a consequence that a real deployment would place the
  vault in a separate store with separate credentials, and that this is the
  deployment difference. Record the measured alternative — the vault in its own
  encrypted DuckDB database, attached only by the tasks that need it, which is a
  separate store with a separate credential and works today — as **deferred to
  M8**, where access control lives and where it is a genuine candidate rather
  than a rejected idea. It is deferred here because `conventions.md` and
  `architecture.md` both fix `meta` as a schema of the one warehouse file, and
  moving it is an access-control decision rather than an ingestion one.
- The salt can never be rotated without re-tokenising all history. Document it
  as a known limitation with the migration it would require.

### 7. Contract validation and quarantine
Per record, against the contract in force:
- A value that fails its declared type, a null in a non-nullable column, or a
  primary key that is null or duplicated within the batch, sends **that record**
  to quarantine with the failing column and the reason. It is never coerced and
  never dropped.
- The landed count plus the quarantined count equals the count read from the
  source, for every batch, always. This is asserted, not assumed.

**What this source can actually violate, measured.** `core` has types, foreign
keys, check constraints and not-null constraints, so most of the above is
unreachable from it, and saying which is which is what stops the control from
looking better tested than it is.

| Failure mode | Reachable from this source |
|---|---|
| Value fails its declared type | **No.** Postgres enforces it. The scripted widening of `core.payments.remittance_reference` from 140 to 280 characters is detectable at the schema level only: every value the generator writes is exactly 13 characters, so no row ever exceeds the narrower declaration |
| Null in a non-nullable column | **Yes, through declared strictness only.** `core.customers.email` is the declared case. Measured at `ci`: the historical load carries 9 nulls out of 500 customers, and sixteen ticks added 5 more |
| Primary key null or duplicated within the batch | **No.** Every `core` and `ref` table has a single-column primary key that is unique and not null, and one windowed read returns each key once |

**The cost of declared strictness is permanent exclusion, and it is accepted
rather than discovered.** A cleared email is never repopulated, so a customer
whose email the source nulls fails the contract on that day and on every
subsequent day their row moves. Those customers stop reaching bronze, and silver
will have an entity whose history ends mid-stream. That is what a quarantine
decision means, it is visible in `dq.quarantine_log` rather than silent, and it
is the first real population M7's reconciliation has to explain. One column is
declared strict rather than two, so the cost stays bounded and attributable.

**The mod-97 checksum stays out of the ingest gate.** `core.payments`
counterparty IBANs fail the checksum on 6,875 of 6,949 rows, because the
generator writes no check digits and `generator_realism.md` records that as a
deliberate unrealism. A contract rule that rejected them would quarantine 98.9
per cent of the entity, which is an outage rather than a control. It is a `dq`
check at M7 with `warn` severity and its measured share.

### 8. Drift detection at ingest
Compare the source's actual columns against the contract:
- **Additive** — a column present in the source and absent from the contract:
  the batch lands, the column is **not** written to bronze, and the observation
  is recorded in `meta.schema_drift_log`. Bronze carries what the contract
  describes; an unknown column is noticed, not silently absorbed.
- **Breaking** — a type change, a removed column, or a changed primary key: the
  **whole batch** is quarantined, the batch is marked `failed`, the watermark
  does not advance, and the task fails. No partial load, at entity grain.

M3 planted two scripted drift events in the tick timeline, one additive at
anchor plus twelve days and one type widening at anchor plus thirty-seven. The
backfill must encounter both and demonstrate both behaviours.

**Only one of the three breaking kinds is reachable from the scripted
timeline.** A removed column and a changed primary key are not scripted, so they
are proven by unit test against a fabricated source schema and by nothing else.
Criterion 13 says so rather than implying three kinds were exercised. Scripting a
column removal and a primary key change is a candidate for a later milestone; it
is a change to the generator's drift timeline, not to this framework.

**A consequence for silver, recorded now rather than discovered at M5.** After
the additive event fires, `core.merchants` rows are revised only in
`merchant_risk_score`, which is the column the contract omits. Bronze therefore
receives `merchants` versions in which nothing visible has changed. Silver must
expect a version whose every contract-described column equals its predecessor's,
and must not treat that as a defect or deduplicate it away. Noted in
`model_inventory.md`.

### 9. Backfill
`make backfill FROM=... TO=...`, driving a loop: tick the source one day, run
`ingest_reference_data` for that day, run `ingest_core_banking` for that day,
wait for registration, repeat. Reference data runs first, so a new code exists
before a fact references it.

The tick and the ingestion stay in separate DAGs and the loop lives in a script,
because the simulation is not part of the platform and coupling them in one DAG
would put that boundary in the wrong place. The reason M3 established — that a
later tick re-stamps rows an earlier one wrote, so the log reconciles only at the
tick — is why the loop cannot be reordered.

**The loop halts on any `failed` batch.** It reports the entity, the drift kind
and the resolution procedure, and stops. It does not bump a contract version:
that is a human decision recorded in a commit, and a backfill that made it
automatically would have removed the control it exists to demonstrate.

Because the halt is immediate, the outage is one day rather than the rest of the
run. The watermark of the failed entity never advanced, the source has ticked
only to the day that failed, and on resume that day is re-extracted cleanly
under the new contract version. Criterion 15 therefore stands unmodified.

**`make backfill` is resumable and idempotent on re-invocation.** It determines
where to start from the simulation state and the registry rather than from an
argument, so re-invoking it over a range it has already completed changes
nothing, and invoking it after a halt continues from the day that failed.
Resumability is demonstrated in its own right in criterion 20, because it is the
property the drift story depends on.

### 10. Airflow
Two DAGs on one framework: `ingest_core_banking` over the sixteen `core`
entities and `ingest_reference_data` over the twenty-nine `ref` entities. Both
use the same watermark mechanism, the same four phases, the same registry and
the same contracts. `ref` tables carry `updated_at` and the audit trigger like
everything else, so most days most reference batches are empty. **An empty
registered batch is a correct outcome and not a special case**: it records that
the entity was asked and had nothing to say.

**`schedule=None`, `catchup=False`, driven entirely by explicit runs.** The
reason belongs in the DAG docstring and in `architecture.md` rather than only
here, and it is a property of this deployment rather than of the design: **the
source's clock is simulated, so a wall-clock schedule has no meaning against
it.** A day of source data exists when a tick has written it, not when the wall
clock passes midnight. The behaviour was measured rather than assumed — a
backfill against a paused DAG creates runs that never leave `queued`, and
unpausing a `catchup=True` DAG with a past start date immediately created and
ran fifteen scheduled runs — so a daily schedule plus catchup would have the
scheduler racing the backfill loop and registering batches for days the tick had
not yet produced.

**A deployed platform would run `ingest_core_banking` daily at `0 4 * * *`
UTC**, after the source's own overnight batch has finished — the simulated
source runs its status sweep at 02:00 and lands its last movements at 23:45 —
extracting the previous day, with `ingest_reference_data` an hour ahead of it.
That schedule is stated so the omission reads as a decision rather than an
oversight.

One asset per entity, emitted by the register step. Pools as described in
section 3. Task-level retries with exponential backoff. An `on_failure_callback`
writing the failure to `ops` through the bounded-retry warehouse helper, because
a callback cannot acquire a pool; it is written so that it cannot itself raise,
since a callback that fails while reporting a failure reports nothing.

**`pool` is never set in `default_args`.** Doing so would give
`warehouse_access` to all sixteen mapped extract tasks and serialise the whole
extraction through one slot. The measured behaviour is that a mapped task with
no declared pool takes `default_pool` with one slot each, and only the tasks
declaring `warehouse_access` take it. The DAG carries a comment at the place
where a pool would otherwise be added.

Version 1 asked for deferrable operators "where waiting is involved". Nothing in
either DAG waits on anything external — the loop that waits is a script outside
them — so the clause is dropped rather than satisfied by an invented sensor.

### 11. Make targets
`warehouse-apply`, `contracts-bootstrap`, `contracts-diff` (with `CHECK=1`),
`extract` (one interval, outside Airflow, for development), `backfill`,
`bronze-stats` (landed and quarantined counts by entity and ingest date).

Every target that touches the warehouse runs inside a container, because the
warehouse file is on a named volume and is not reachable from the host by
design. `pyarrow` and `boto3` become host dependencies in `pyproject.toml`: they
are present in the Airflow image already, and `make extract` needs them outside
it.

### 12. Tests
- Unit: contract validation per failure mode, including the two breaking drift
  kinds the scripted timeline cannot reach; tokenisation determinism across
  entity, column and batch; batch id sequencing including the already-registered
  case, the reused-unregistered case and the cleared case; watermark arithmetic
  including the overlap; quarantine routing; drift classification; declared
  strictness distinguished from structural divergence.
- Integration: a single interval end to end; the idempotency cases in section 4;
  the overlap re-reading a row that shares the watermark instant; both drift
  behaviours; an empty reference batch registering cleanly; reconciliation
  against `platform.tick_log`.
- DAG integrity: neither DAG sets `pool` in `default_args`; the mapped extract
  tasks hold no pool and the open, register and gate tasks hold
  `warehouse_access`; both DAGs are unscheduled and have catchup off.

## Out of scope
Silver, dbt, the external feeds, FX conversion, sanctions screening, GDPR
erasure, lineage. Also out of scope and owned elsewhere: the primary-key
reconciliation that detects a physical delete and the orphaned-object reaper,
both M7; compaction and retention of bronze partitions, M7; moving the vault to
a separate encrypted store, M8; and scripting a column removal or a primary key
change in the generator's drift timeline, a later milestone.

## Acceptance criteria
1. `make warehouse-apply` creates every table idempotently; running it twice
   changes nothing.
2. Contracts exist for all forty-five source entities, sixteen in `core` and
   twenty-nine in `ref`; `make contracts-diff` reports zero structural
   divergence at the current commit, and reports the one declared strictness
   with its stated reason.
3. One extraction run lands Parquet at the specified key and records landed and
   quarantined counts in the registry.
4. The watermark advances only on registration. Prove it by failing the register
   step and showing the watermark unmoved and the batch left `written`.
5. A retry within an unregistered batch reuses the batch id and produces
   byte-identical objects.
6. A re-run after registration allocates the next sequence, and the earlier
   partition is unmodified. Prove both.
7. The overlap window re-reads rows sharing the watermark instant. Prove it
   primarily on `accounts`, where the last movement bucket puts rows on the
   daily watermark instant every day, and additionally on `cards`, where the
   deliberate batched status sweep M3 writes at 02:00 is itself the watermark:
   show a row appearing in two consecutive batches rather than being lost.
8. No cleartext identifier appears in any bronze object. Enumerate the values
   from `meta.pii_vault`, which is their designated home, and scan every Parquet
   file byte-wise rather than column-wise, because Parquet writes column
   statistics and dictionary pages that a column-level scan would not see.
   Report `core.transactions.counterparty_reference` as a column the check
   proves nothing about, because it is null in every row the source produces.
9. Tokenisation is deterministic: the same raw value yields the same token across
   entities, columns and batches. Prove across at least three entities, from
   real data rather than by construction, using the values `customers.full_name`
   shares with `payments.counterparty_name` and `accounts.iban` shares with
   `payments.counterparty_iban`.
10. The vault holds exactly one row per distinct identifier value, keyed on the
    token, with the entity, column and batch that first saw it.
11. Declared strictness quarantines real records: the nulled
    `core.customers.email` rows are quarantined with their reason, and the counts
    are reported as observed rather than injected. An injected violation covers
    the failure modes this source cannot reach, and an identifier column's
    offending value is quarantined as a token, never raw.
12. Additive drift lands the batch, omits the unknown column from bronze, and
    logs the observation.
13. Breaking drift quarantines the whole batch, marks it `failed`, leaves the
    watermark unmoved, and fails the task. Nothing partial lands for that entity,
    and the entities that wrote cleanly in the same run are still registered.
    The type change is proven end to end against the scripted event; the removed
    column and the changed primary key are proven by unit test only, and the
    evidence says so.
14. Landed plus quarantined equals rows read from source, for every batch in a
    sixty-day backfill.
15. Reconciliation against `platform.tick_log` is exact for every tick in the
    backfill, per entity.
16. Late-arriving rows land in the ingest-date partition of their arrival, not
    their business date. Report the counts that prove it.
17. A sixty-day backfill completes, encounters both scripted drift events, and
    every batch is `registered` or explicitly `failed` with a reason.
18. Assets are emitted per entity by both DAGs and visible in Airflow.
19. All existing checks pass; delivered as a pull request on
    `feat/M4-bronze-landing` with all four required checks green.
20. The backfill halts on a `failed` batch, naming the entity, the drift kind
    and the resolution procedure; after the contract version is bumped in a
    commit, `make backfill` resumes from the day that failed and completes.
    Re-invoking it over an already-completed range changes nothing.
21. `ingest_reference_data` runs ahead of `ingest_core_banking` on every day of
    the backfill, and an empty reference batch is registered with zero rows
    landed and zero quarantined.

## Changelog

Version 2 replaces version 1 under the reissue protocol in
`docs/specs/README.md`: the review of version 1 produced more than five
corrections to a specification that had not been implemented, so the file is
replaced rather than amended. Every correction below was ruled on before any
implementation began, and most of them follow from probes run against the
running stack rather than from reading.

**Scope.**
- The twenty-nine `ref` entities come into scope. Silver cannot resolve
  `is_posted`, `is_open`, `is_customer_initiated` or `implies_default` without
  them, and deferring reference data to the external-feeds specification would
  put it behind an unrelated milestone. A second DAG, `ingest_reference_data`,
  reuses the framework unchanged. Criterion 2 becomes forty-five contracts, and
  criterion 21 is new.
- `platform.column_classifications` is stated to be read and not landed, so that
  the simulation's own bookkeeping is visibly outside the extraction boundary.

**Orchestration.**
- Section 10: `schedule=None` and `catchup=False`, driven by explicit runs. A
  backfill against a paused DAG was measured to leave its runs `queued`
  indefinitely, and unpausing a `catchup=True` DAG with a past start date was
  measured to create and run fifteen scheduled runs at once. A daily schedule
  and the section 9 loop are therefore two mechanisms creating runs for the same
  dates. The schedule a deployed platform would use is stated so the omission
  reads as a decision.
- Section 3: a fourth phase. Register runs on `all_done` and registers only the
  batches that wrote, and a terminal gate fails the run. Under `all_success` one
  entity's breaking drift would have left the other fifteen unregistered and
  their watermarks unmoved, killing the pipeline from that day forward. "No
  partial load" is restated as a guarantee at entity grain.
- Section 10: `pool` is never set in `default_args`, with the measurement that
  makes it matter; and the deferrable clause is dropped, because nothing in
  either DAG waits on anything external.

**Batch identity.**
- Section 4: the sequencing rule is rewritten to depend on registry status alone
  and on neither `run_id` nor `try_number`. Measured in Airflow 3.3.2: a retry
  changes the task instance UUID, and a backfill re-run of a completed logical
  date reuses the same `run_id` and clears its task instances, so it is
  indistinguishable from a retry by run identity. Version 1's "next unused
  sequence" would have allocated a new sequence for a retried open task.
- Section 4: `interval_start` is recorded as the logical date at midnight, with
  the measurement that Airflow 3's cron timetable carries no data interval and
  that a manual trigger's interval is the wall clock at trigger. The format is
  unchanged.

**Correctness of the criteria as written.**
- Section 5: `_ingested_at` becomes a batch property read back from the
  registry. Version 1 required it per record and also required byte-identical
  objects on retry, which cannot both hold.
- Section 5: the extract query orders by the primary key. Criterion 5 silently
  assumed a row order that nothing specified.
- Section 1 and criterion 10: the vault is keyed on the token, with entity and
  column recording first sighting. Measured: 949 payments carry a
  `counterparty_name` equal to some customer's `full_name`, so a key including
  entity and column would have produced two rows for one value and made
  criterion 10 unsatisfiable. The same measurement lets criterion 9 be proven
  from real data instead of by construction.
- Criterion 7 names `accounts` as the primary instrument. Measured over sixteen
  `ci` ticks: the 02:00 sweep is the daily watermark for `cards` on three days of
  sixteen, for `merchants` on four of four and for `loan_installments` on three
  of five, but never for `accounts`, `customers`, `payments` or `transactions`,
  where the movement phase stamps later. `accounts` put 546 rows on the daily
  watermark instant across those sixteen days, so it proves the property every
  day; `cards` proves it with a status-sweep row, as version 1 asked.
- Criterion 8 states the method: enumerate from the vault, scan byte-wise, and
  report the column the check proves nothing about.

**Validation and quarantine.**
- Section 2: `contracts-diff` distinguishes structural divergence from declared
  strictness, and `CHECK=1` fails only on the former, folded into the existing
  `docs` job. A contract is an agreement about what the platform will accept,
  not a mirror of the source.
- Section 7: `core.customers.email` is declared non-nullable, so the quarantine
  path has real traffic. Measured, version 1's quarantine counts would have been
  zero for every batch of the backfill: Postgres enforces every type, every
  primary key is unique and not null, and the scripted widening produces no
  value longer than the narrower declaration. A control with nothing to catch is
  worse than none. The permanent-exclusion cost of the declaration is stated.
- Section 7: the mod-97 IBAN rule is explicitly excluded from the ingest gate
  and assigned to M7. Measured: 6,875 of 6,949 payments would fail it.
- Section 8 and criterion 13: two of the three breaking drift kinds are
  unreachable from the scripted timeline and are proven by unit test only, which
  the criterion now says rather than implies.
- Section 8: the additive event's consequence for silver — a `merchants` version
  in which no contract-described column changed — is recorded.

**Backfill.**
- Section 9: the loop halts on a `failed` batch and reports the resolution
  procedure; it does not bump a contract version, because that decision is the
  control working and belongs in a commit. Version 1 left the twenty-three days
  after the widening undefined; halting immediately makes the outage one day, so
  criterion 15 stands unmodified.
- Section 9: `make backfill` is resumable and idempotent on re-invocation, and
  criterion 20 is new, because resumability is what the drift story depends on.

**The vault at rest.**
- Section 6: the decision is unchanged and its reason is replaced. Version 1
  argued that ciphertext and key would share one database, which is not true of
  this stack: DuckDB 1.5.5 was measured to support `ATTACH … (ENCRYPTION_KEY …)`
  with the key supplied from the environment, cleartext absent from the file
  bytes, and an unkeyed open refused. The reason that survives measurement is
  narrower: a second layer inside the same file defends only against offline
  exfiltration, the separate-store mitigation is the one that addresses the
  threat, and the layer adds an irreversible-loss mode for a fraction of the
  benefit. The measured alternative is recorded as deferred to M8 rather than
  rejected.

**Naming and documentation.**
- Section 2's "deliberate violation of one source per fact" framing is removed.
  The dictionary records what the source is and the contract records what the
  platform has agreed to accept; those are two facts, and calling them one
  invites a future reader to delete a copy and remove the control. Each contract
  records the dictionary revision it was bootstrapped from.
- The table names in this specification win over `model_inventory.md` and
  `traceability.md`, which are amended: `ops.extract_watermark`,
  `meta.contract_version` and one `dq.quarantine_log` rather than
  `ops.watermarks`, `meta.contract_versions` and sixteen
  `bronze.quarantine_<entity>` tables.
- Section 5: `_source_file` keeps its name for a relational source and carries
  the qualified relation, documented rather than inferred.
- Section 1 and section 11: warehouse-touching targets run inside a container;
  `pyarrow` and `boto3` become host dependencies.
- Section 1: `ops.source_reconciliation`'s landed count is scoped by the row's
  `updated_at` date and is not the registry's batch count, because a batch spans
  the overlap window and a source day does not. Conflating them would make
  criteria 14 and 15 contradict each other.
- Section 3: asset emission is stated to happen after the commit and not to be
  transactional with it, with what that costs and where detecting the gap
  belongs.
- Section 2: entity names are unique across `core` and `ref`, so the contract
  directory stays flat, and the bootstrap refuses a collision rather than
  leaving it to be found in the lake.
