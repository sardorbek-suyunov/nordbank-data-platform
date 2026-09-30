# 007 — dbt Project and Bronze Models

Status: Approved
Version: 2
Supersedes: version 1 (the file as first committed on this branch)
Depends on: 000–006

## Goal
Stand up the dbt project and expose every landed entity as a bronze model: a
view over the lake that reads only batches the platform registered. Publish
the project's documentation to GitHub Pages, and run a `ci`-scale ingestion and
dbt build in CI, so that bronze is exercised on every pull request.

## Context
The `dbt/` tree is a skeleton of README files. Specifications 005 and 006 land
50 entities as Parquet under
`bronze/<source>/<entity>/ingest_date=/batch_id=/`, register each batch in
`ops.batch_registry`, and can leave objects of failed or interrupted runs in
the bucket. Reading the lake without the registered-batch filter reads the
output of runs that died.

Planning measured four facts this specification is built on:
- **The writer infers types.** It infers Parquet types from values rather than
  applying the contract, so 13 of the 49 entities with objects carry between 2
  and 7 physical schemas. The causes are decimal precision chosen per file and
  all-null columns written as Arrow `null`. Only settlements differs because of
  a contract version.
- **DuckDB 1.5.5 needs `union_by_name`.** It matches columns by name. Without
  `union_by_name` the first file's schema wins, a later file missing a column
  errors, and a column absent from the first file is silently dropped. With it,
  every entity reads, at the widest type, with no value changed.
- **An empty glob fails.** It fails at view creation and at query time, and no
  SQL-level fallback exists.
- **A view re-lists objects on every query**, so a view sees new data without
  being rebuilt.

Inherited, not re-decided:
- the `warehouse_access` pool;
- the contracts as the one source for columns, types, keys and
  classifications;
- the CI anchor moving to `ACCEPTANCE_ANCHOR` (ADRs 0014 and 0016).

## Scope

### 1. Ingestion fixes this depends on
- **The writer writes the contract's schema.** It no longer infers types from
  values. An ingest-integrity check asserts that no entity carries more
  physical schemas than it has contract versions.
- **Vault completeness.** An ingest-integrity check asserts that every
  identifier token in registered bronze has a vault row.
- Each fix is its own commit, measured before and after, and each check is
  proven able to fail.

### 2. The dbt project
- `dbt-core` and `dbt-duckdb` in a `dbt` dependency group.
- **DuckDB is pinned once, in the lockfile.** The image's requirement files are
  exported from the lock, and a CI check fails if they disagree. Extensions are
  keyed to the DuckDB version, so one pin is what keeps the build-time
  extension loadable.
- **dbt runs in its own virtual environment in the image.** dbt's dependencies
  do not resolve under Airflow 3.3.2's constraints.
- **No dbt packages.** `dbt deps` fetches from the network.
- `dbt/profiles.yml` is committed, with one target, `warehouse`, reading
  everything from the environment.
- MinIO credentials reach DuckDB only through the profile's secrets block, in
  `DBT_ENV_SECRET_` variables, never through a hook, a model or a macro.
  Measured: a hook's rendered secret lands in `manifest.json`, `run_results.json`
  and `dbt.log`.
- `httpfs` is installed at image build into an explicit extension directory,
  and automatic extension install is off.

### 3. Bronze models
- One model per contract entity, named `br_<source>__<entity>` from the lake
  prefix: 50 at the time of writing.
- Materialised as views in the `bronze` schema.
- **Read only through the registered-batch macro:**
  - It reads the entity's objects by name (`union_by_name`) and casts every
    column explicitly.
  - It keeps only rows whose object key's `batch_id` is registered. The file's
    `_batch_id` column is not the filter: a planted copy of a real file passes
    a filter on it.
  - A static check fails the build if any bronze model reads the lake another
    way.
- **Columns across contract versions:**
  - A model exposes the union of every contract version's columns.
  - A retired column stays, null for deliveries after its retirement, and is
    described by delivery date.
  - Each column takes the type of the latest version that carries it.
  - Identifier columns are typed and described as tokens, with the width read
    from the tokeniser, not as the cleartext type the contract describes.
- **An entity with no registered batch with rows landed** builds as a typed
  empty relation. This is decided at build time, because an empty glob cannot
  be read. Registered empty batches write no object. No sentinel object is
  written to the lake, because every object belongs to a batch.
- API and file entities expose `_raw_payload`.

### 4. Generated, not hand-written
One generator, `make dbt-generate`, produces from the contracts:
- the model files;
- the model YAML: columns, types, descriptions, classification in `meta` and
  in the description, grain and tests;
- the `ops.batch_registry` source;
- the bronze section of `model_inventory.md`, between markers.

Generated files are committed. `CHECK=1` fails in CI on any difference.
`docs/README.md` is amended to allow sections generated between markers, each
with a mechanical check. The inventory gains `br_cardnet__settlement_totals`
with its consumer, the Q15 mart.

### 5. Tests on every bronze model
- **Grain:** the primary key plus `_batch_id`, `unique` and `not_null`,
  severity `error`.
- **Landing reconciliation:** the model's rows per batch equal the registry's
  `rows_landed` for every registered batch, zero-row batches included, severity
  `error`.
- **Tokens:** every `identifier` column holds values of the tokeniser's format,
  severity `error`.
- **Hive-key consistency:** the object key's `batch_id` equals `_batch_id`, and
  its `ingest_date` equals the registry's.
- Audit columns `not_null`.
- `store_failures` is never set, enforced by a check.
- Every generic test is a macro in `dbt/macros/`. `make dbt-prove` builds
  planted fixtures and requires every test to fail on them.

### 6. Sources and lineage
`ops.batch_registry` is a dbt source. `meta.pii_vault` is never declared and
never read by dbt. Other `ops`, `dq` and `meta` tables are declared only if a
model or test reads them.

### 7. Orchestration
`transform_bronze` runs `dbt build` over bronze, as a subprocess, in the
`warehouse_access` pool, with `max_active_runs=1`.
- It is scheduled on the six ingestion aliases joined with `|`. Measured: a
  list of aliases never fires while one feed has no events.
- **Decision rule:** if it slows the 61-day backfill by more than 15 per cent of
  wall time, the backfill pauses it and runs it once at the end. Daily
  operation stays asset-scheduled. Report the run count, the build count and
  the slowdown either way.

### 8. CI
The `stack` workflow, in this order:
1. seed at `ACCEPTANCE_ANCHOR` and run the integration tests;
2. reseed;
3. backfill 2026-07-20 to 2026-07-26 through the real DAGs;
4. plant an unregistered object;
5. `dbt build` with `--warn-error`;
6. tick to the drift day and run `schema-check`.

- The window is fixed by coverage: the late file arrives on its last day.
  - *Covered:* the initial load, an FX weekend, two no-arrival days, a late
    file, a correction, quarantined records and a sanctions snapshot.
  - *Not covered:* drift, a second contract version, parking, a failed batch,
    FRED and a second snapshot. These are exercised by the local acceptance
    run only.
- **No live external call.** Frankfurter responses for the window are recorded
  once and served when an environment variable names the fixture directory.
  The live URL points at an unroutable host in CI, so a live call fails loudly.
- The runner's time per ingested day and the job's duration are reported. If
  the job nears its timeout, the loop moves from `docker compose exec` to the
  REST helper. The window is not shortened.
- The committed `ci` manifest regenerates at `ACCEPTANCE_ANCHOR` in one commit
  whose body follows the manifest rule.
- The four required checks are unchanged.

### 9. Documentation on GitHub Pages
- A deploy job in the `stack` workflow runs on pushes to `main` only, needs
  `stack`, and receives the built artefact.
- Before upload, the artefact is scanned for every environment secret and every
  vault value. The scan fails on a hit and is proven by a planted value.
- `dbt docs generate --static`.
- The published site shows every column's classification, verified by grepping
  the built site.

### 10. Documents
- **An ADR: bronze as registry-filtered views over the lake.**
  - *Rejected:* materialised bronze tables, which would be a second copy of the
    lake; an unfiltered read; a filter on the file's `_batch_id`.
  - *Negative consequence:* every read lists the prefix and reads every
    object's footer. Measured at `ci`: 2.46 s and 3,017 requests over 2,968
    objects. Projected for `full`.
- **`architecture.md`:**
  - the `ci` profile's timing, replaced with measured figures;
  - the macro indicators load pattern, corrected to a full re-request with
    append;
  - the bronze view and its filter.
- `docs/runbook.md` and `docs/README.md`.
- The M4 checkpoint and `project_state.md`. This specification closes M4.

## Out of scope
Silver and above. Quarantine models. The `dq` framework. The tie-cluster
cursor. Compaction and retention. The BigQuery target.

## Acceptance criteria
1. `dbt build --warn-error` over bronze passes on the `ci` warehouse, locally
   and in CI, with nothing silenced.
2. The number of bronze models equals the number of contract entities, 50,
   stated as a number in the test.
3. Every bronze model reads the lake only through the macro, enforced by a
   check proven able to fail.
4. A planted object under an unregistered batch id, and a planted copy of a
   registered file under a new key, are absent from the model. This is shown
   on a throwaway stack and in CI.
5. Model rows equal the registry's `rows_landed` for every registered batch,
   on the full local acceptance history and in CI.
6. All 13 multi-schema entities read correctly. Settlements rows before and
   after the `merchant_name` retirement both appear, populated before and null
   after, by delivery date.
7. An entity with no registered rows builds as an empty relation of the right
   columns and types, and the build passes.
8. The grain, reconciliation, token and Hive-key tests pass on every model, and
   `make dbt-prove` shows each failing on a planted fixture.
9. `make dbt-generate CHECK=1` passes, and fails on a hand edit or on an
   unregenerated contract change.
10. No hook, model or macro contains a credential. The published artefact scan
    is clean, and catches a planted value.
11. DuckDB is pinned once. The image's exports agree with the lock, checked in
    CI.
12. `httpfs` loads with `--network none` from the build-time install, and
    automatic install is off.
13. `transform_bronze` runs in the pool on the six aliases joined with `|`. The
    backfill's run count, build count and slowdown are reported, and the
    decision rule applied.
14. CI ingests the window with no live external call and runs `dbt build`.
    Coverage and the job's duration are reported.
15. The `ci` manifest is regenerated at `ACCEPTANCE_ANCHOR` in one commit
    following the manifest rule.
16. The documentation is published to GitHub Pages from `main`, showing every
    column's classification.
17. The writer writes the contract's schema, and both new ingest-integrity
    checks pass and are proven able to fail.
18. The ADR, the document corrections and the generated inventory section are
    in place.
19. The M4 checkpoint is written and `project_state.md` shows M4 closed.
20. All four checks pass; delivered as a pull request on `feat/M4-dbt-bronze`.

## Changelog
- The writer's inferred types, not contract versions, are what make physical
  schemas differ. The writer is fixed, and the models read by name regardless.
  Measured at planning.
- DuckDB is pinned once in the lockfile, with checked exports, instead of
  "pinned in the lockfile" with two independent pins in practice.
- dbt runs in its own environment: measured, its dependencies do not resolve
  under Airflow's constraints.
- One target instead of three identical ones; variables arrive at M5.
- The filter uses the object key's `batch_id`, not the file's `_batch_id`.
  Measured: a planted copy passes the latter.
- "No registered batch" becomes "no registered batch with rows landed",
  decided at build time.
- Identifier columns are typed as tokens, not as the contract's cleartext type.
- The registry column is `rows_landed`, not `records_landed`.
- Criterion 4 is proven by planting; the evidence lake has no orphans.
- Generated documentation sections are permitted by an amendment to
  `docs/README.md`.
- The schedule is the six aliases joined with `|`. Measured: a list never
  fires.
- The backfill's treatment of `transform_bronze` is decided by a stated rule,
  not left open.
- The CI window is fixed by coverage at seven days, and its ordering is
  stated.
- Pages is deployed from a job in `stack`, not from a workflow that would
  rebuild the stack a second time.
- The classification is carried in the description as well as `meta`, and the
  built site is verified.
- Vault completeness is added as an ingest-integrity check, following the T11
  review.

## Amendments
