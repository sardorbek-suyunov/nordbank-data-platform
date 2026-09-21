# 005 — Extraction and Bronze Landing

Status: Approved
Depends on: 000, 001, 002, 003, 004

## Goal
Extract the core banking source incrementally by watermark, validate against a
contract, tokenise identifiers, land immutable Parquet in the lake, and record
every batch in the warehouse's operational schema. This is the first half of M4
and it establishes the framework the external feeds reuse.

## Scope

### 1. Warehouse operational schema
Numbered idempotent SQL under `infra/warehouse/schema/`, applied by
`make warehouse-apply`. These are platform state written by Python, not derived
models, so they are not dbt's concern.

- `ops.extract_watermark` — one row per source system and entity: the highest
  `updated_at` successfully registered.
- `ops.batch_registry` — one row per batch: batch id, source system, entity,
  ingest date, data interval, watermark from and to, rows landed, rows
  quarantined, object prefix, status, timings, and the triggering run id.
  Status is `open`, `written`, `registered` or `failed`. **Only `registered`
  batches are readable downstream.**
- `ops.source_reconciliation` — per entity per source day: counts claimed by
  the source, landed, quarantined, and the difference.
- `meta.pii_vault` — token to raw value, with classification, source entity and
  column, and the batch that first saw it.
- `meta.contract_version` — the contract version in force per entity, and when.
- `meta.schema_drift_log` — drift observed at ingest: entity, column, kind,
  batch, action taken.
- `dq.quarantine_log` — one row per rejected record: batch, entity, column,
  reason, and the offending value **tokenised if the column is an identifier**.

### 2. Contracts
`contracts/corebank/<entity>.yml`: contract version, the watermark column, the
primary key, and per column its name, type, nullability and classification.

The contract is the platform's understanding of the source and it deliberately
lags reality — that lag is what makes drift detectable. So it is **not**
generated from the data dictionary on every run. Instead:

- `make contracts-bootstrap` generates a contract from the dictionary for any
  entity that has none. A one-time bootstrap, not a sync.
- Thereafter contracts are hand-authored and version-bumped deliberately.
- `make contracts-diff` reports divergence between each contract and the current
  dictionary. It reports; it does not fail. Drift at ingest is the real control.

State that reasoning in the spec and in `architecture.md`, because the standing
rule is one source per fact and this is a deliberate exception with a purpose.

### 3. Extraction, in three phases
The warehouse is single-writer and all access serialises through the
`warehouse_access` pool, so extraction must not hold the pool while reading
Postgres or writing to the lake.

1. **Open** — one pooled task: read every entity's watermark, allocate a batch
   id per entity, insert `open` rows into the registry. One transaction.
2. **Extract** — one mapped task per entity, no warehouse access: read from
   Postgres as `nordbank_reader` where
   `updated_at >= watermark - EXTRACT_LAG`, validate against the contract,
   tokenise identifier columns, write Parquet to the lake, write quarantine
   records. Reports its counts and its observed maximum `updated_at`.
3. **Register** — one pooled task: in a single transaction, mark batches
   `registered`, advance watermarks to the observed maximum, write the
   reconciliation rows, and emit the per-entity assets. **The watermark advances
   here and nowhere else.** A failure leaves every batch `written` and every
   watermark unmoved, so the next run repeats rather than skips.

### 4. Batch identity and re-runs
Batch id is `{entity}-{interval_start:%Y%m%dT%H%M%S}-{seq:02d}`, where `seq` is
the next unused sequence for that entity and interval, determined at open time
from the registry.

- A task retry or a cleared task within an unregistered batch reuses the same
  batch id and overwrites its own objects. Idempotent.
- A re-run of an interval whose batch is already `registered` allocates the next
  sequence, so the registered partition is never modified. Silver deduplicates
  the overlap.
- The register step refuses to transition a batch that is already `registered`,
  so immutability is enforced rather than assumed.

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
  silver; `sensitive` is retained and access-controlled.
- The vault records token to raw value with the batch that first saw it.
  Insert-if-absent, so re-extraction does not duplicate.
- **The vault stores the raw value, and the mapping is the key material.**
  Erasure deletes the row, which is what makes the surviving tokens
  unresolvable. Do not add a second encryption layer over the raw value: in a
  single-node deployment the ciphertext and its key would share a database and a
  credential, so it would be key-management theatre. Record in ADR 0005 as a
  consequence that a real deployment would place the vault in a separate store
  with separate credentials, and that this is the deployment difference.
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

### 8. Drift detection at ingest
Compare the source's actual columns against the contract:
- **Additive** — a column present in the source and absent from the contract:
  the batch lands, the column is **not** written to bronze, and the observation
  is recorded in `meta.schema_drift_log`. Bronze carries what the contract
  describes; an unknown column is noticed, not silently absorbed.
- **Breaking** — a type change, a removed column, or a changed primary key: the
  **whole batch** is quarantined, the batch is marked `failed`, the watermark
  does not advance, and the task fails. No partial load.

M3 planted two scripted drift events in the tick timeline, one additive and one
type widening. The backfill must encounter both and demonstrate both behaviours.

### 9. Backfill
`make backfill FROM=... TO=...`, driving a loop: tick the source one day,
trigger `ingest_core_banking` for that day, wait for registration, repeat.

The tick and the ingestion stay in separate DAGs and the loop lives in a script,
because the simulation is not part of the platform and coupling them in one DAG
would put that boundary in the wrong place. The reason M3 established — that a
later tick re-stamps rows an earlier one wrote, so the log reconciles only at the
tick — is why the loop cannot be reordered.

### 10. Airflow
`ingest_core_banking`, daily, catchup enabled, one asset per entity. Deferrable
where waiting is involved. Pools as described. Task-level retries with
exponential backoff. An `on_failure_callback` writing the failure to `ops`.

### 11. Make targets
`warehouse-apply`, `contracts-bootstrap`, `contracts-diff`, `extract`
(one interval, outside Airflow, for development), `backfill`, `bronze-stats`
(landed and quarantined counts by entity and ingest date).

### 12. Tests
- Unit: contract validation per failure mode, tokenisation determinism, batch id
  sequencing including the already-registered case, watermark arithmetic
  including the overlap, quarantine routing, drift classification.
- Integration: a single interval end to end; the idempotency cases in section 4;
  the overlap re-reading a row that shares the watermark instant; both drift
  behaviours; reconciliation against `platform.tick_log`.

## Out of scope
Silver, dbt, the external feeds, FX conversion, sanctions screening, GDPR
erasure, lineage.

## Acceptance criteria
1. `make warehouse-apply` creates every table idempotently; running it twice
   changes nothing.
2. Contracts exist for all sixteen core entities; `make contracts-diff` reports
   zero divergence at the current commit.
3. One extraction run lands Parquet at the specified key and records landed and
   quarantined counts in the registry.
4. The watermark advances only on registration. Prove it by failing the register
   step and showing the watermark unmoved and the batch left `written`.
5. A retry within an unregistered batch reuses the batch id and produces
   byte-identical objects.
6. A re-run after registration allocates the next sequence, and the earlier
   partition is unmodified. Prove both.
7. The overlap window re-reads rows sharing the watermark instant. Use the
   deliberate batched status sweep M3 writes at 02:00: show a row appearing in
   two consecutive batches rather than being lost.
8. No cleartext identifier appears in any bronze object. Prove by scanning every
   Parquet file for the raw values of a known set of identifiers.
9. Tokenisation is deterministic: the same raw value yields the same token across
   entities, columns and batches. Prove across at least three entities.
10. The vault holds exactly one row per distinct identifier value, with the batch
    that first saw it.
11. An injected contract violation quarantines that record with its reason, and
    an identifier column's offending value is quarantined as a token, never raw.
12. Additive drift lands the batch, omits the unknown column from bronze, and
    logs the observation.
13. Breaking drift quarantines the whole batch, marks it `failed`, leaves the
    watermark unmoved, and fails the task. Nothing partial lands.
14. Landed plus quarantined equals rows read from source, for every batch in a
    sixty-day backfill.
15. Reconciliation against `platform.tick_log` is exact for every tick in the
    backfill, per entity.
16. Late-arriving rows land in the ingest-date partition of their arrival, not
    their business date. Report the counts that prove it.
17. A sixty-day backfill completes, encounters both scripted drift events, and
    every batch is `registered` or explicitly `failed` with a reason.
18. Assets are emitted per entity and visible in Airflow.
19. All existing checks pass; delivered as a pull request on
    `feat/M4-bronze-landing` with all four required checks green.
