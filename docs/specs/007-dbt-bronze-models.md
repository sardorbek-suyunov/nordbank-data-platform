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

Appended during implementation. The scope text above is left as issued; the protocol is in
`docs/specs/README.md`.

### 2026-09-30 — T11, measured live before anything else

Airflow's XCom table, a data-only dump of its whole metadata database and every task log on the
fourth acceptance run's stack were searched for every vault value with the PII scan's matcher:
43,950 XCom rows, 17.7 MB and 3,916 files, no hit in any, and a value planted in an XCom-shaped
value and in a log line was caught. The code says why: register re-reads identifier values from
the source or the checksum-verified delivery, and XCom carries batch ids, keys, checksums, counts
and dates. The vault completeness check that followed (section 1) reports 0 unresolved of 20,015
distinct tokens on that run.

### 2026-09-30 — what the build needed that section 1 to 9 did not say

- **The writer's `json` columns are JSON text in the Arrow JSON type**, the Parquet JSON logical
  type DuckDB reads as `JSON`: a column's physical type has to be one type whatever the value's
  shape. `feeds_acceptance.py` decodes it. Quarantine objects are written in an explicit schema
  too, because the writer has one path.
- **Every model carries two more platform columns**, `_object_batch_id` and
  `_object_ingest_date`: the key the filter reads, and what the Hive-key test compares.
- **`_raw_payload` takes the most protective class among the columns it carries**, and the token
  test is not attached to it.
- **A feed column's description is generated from its contract** — the source of truth, where in
  the delivery it comes from, and its class. The feed contracts carry no column descriptions, and
  adding them would change files warehouses have recorded. A `core` or `ref` column takes the
  data dictionary's.
- **A column no object carries yet is selected as a typed null**, decided at build, like the
  empty entity; ADR 0018 records it as the second case of a view built stale.
- **`not_null` is the project's own macro** over a list of columns, so every generic test is in
  `dbt/macros/` as section 5 asks.
- **The inventory's consumer for `br_cardnet__settlement_totals`** is a planned silver model,
  `sl_card_settlement_totals`, serving Q15, added to the silver table: silver is one model per
  source entity, and the mart reads silver.
- **The runner is `nordbank_ops/transform.py`**, not `dbt.py`: Airflow's plugin loader registers
  every plugin file under its bare name, so `dbt.py` would have become `sys.modules['dbt']`. A test
  now refuses a plugin module named like a package the platform imports.
- **`make ingest-integrity`'s sixth check also fails when it reads no token** from batches that
  should carry identifiers: a check that read nothing proves nothing.
- **`make dbt-prove` runs in the lint job**, natively: it needs dbt and DuckDB, not the stack.
- **`FEED_FIXTURE_DIR` serves any interval feed**, and `make feeds-probe` takes `--only`.

### 2026-09-30 — dbt 1.12 reads `.env`

dbt 1.12 loads the first `.env` above its working directory on import (`find_dotenv` with
`usecwd`). On the host it read the repository's, whose M0 placeholder `DBT_TARGET=duckdb` named a
target the profile does not have. The three stale `DBT_` entries are gone from the template;
`make dbt-prove` runs from a temporary directory; the runbook says to run any other host dbt
command from outside the repository. Inside the stack no `.env` lies above the project.

### 2026-09-30 — the SQL lint does not lint generated models or macros

`.sqlfluff` said the templater would switch to dbt at M4. It does not: the dbt templater compiles
against a live warehouse and lake, because the read macro asks the registry at compile time which
entities have landed rows, and the lint job has neither. The generated models are checked by
`make dbt-generate CHECK=1`, and the macros' rendered SQL by `dbt build --warn-error` and
`make dbt-prove`. Hand-written dbt SQL, from silver on, is linted.

### 2026-09-30 — the stack job's first ingestion, and what it changed

- **The generator's integration tests still seeded at 2026-09-18** and failed against the
  manifest regenerated at the acceptance anchor, and the invariant run read the job's anchor
  against the wrong book. Both modules take the anchor from the drift timeline now.
- **`transform_bronze` and the loop's registry reads collided.** A build triggered by the day's
  reference data held the warehouse file while the loop, reading the registry from inside the
  scheduler, gave up after the default 7.5 seconds of backoff: the pool governs only Airflow.
  The loop's reads now poll every two seconds for up to ten minutes, and dbt retries its own
  connection on DuckDB's lock error.
- **The scan's copy of `.env` outlived the scan.** `docker cp` creates root-owned files the
  airflow user cannot delete from `/tmp`; root removes the copy now, and the recipe fails if it
  survives.
- **The job took 28 minutes 38 seconds of its 30**, the week 1,370 seconds, 195 a day, so the
  loop moved from `docker compose exec` to the REST helper, as ruled. Through the REST helper the week took 878 seconds, 125 a day, and the job 20 minutes 25 seconds.

### 2026-09-30 — the evidence stack was stopped while this was built

The fourth acceptance run's stack mounts the working tree, so its DAG processor would have parsed
the branch's DAGs. It was stopped with its volumes kept after the T11 scan and read-only
measurements; nothing in it was written.

### 2026-09-30 — section 7's rule, applied: bronze is built once at the end of a backfill

Measured on a throwaway stack seeded at the acceptance anchor, both runs on the same commit with
the REST loop, over 2026-07-20 to 2026-09-18:

| | `transform_bronze` active | paused |
|---|---|---|
| Wall time | 4,649 s, 76 s a day | 3,600 s, 59 s a day |
| Ingestion runs that registered batches | 253 | 253 |
| Builds | 243, all successful, 10 coalesced | none |
| A build | median 9.2 s, at most 12.1 s, 38 minutes in all | |
| Pooled ingestion tasks' wait for the slot | median 1.9 s, 23.9 minutes in all | |

The slowdown is 29.1 per cent, over the 15 per cent the rule allows, so the backfill pauses
`transform_bronze` while it runs, unpauses it whatever happens, triggers one build with no logical
date, and reports the window complete when that build has succeeded; one build of the whole
history took 31 seconds. Daily operation keeps the schedule. The active run is also the evidence
that the schedule fires: before it, a build on the empty warehouse made fifty typed empty views;
after the first day's registrations, `transform_bronze` rebuilt the forty-nine whose entities had
landed rows to read the lake, and FRED stayed empty. The stack job now builds once at the end of
its week, so the firing of the schedule is shown by the local run and the DAG tests, not by CI.
With both changes the job took 14 minutes 32 seconds, the week 543 seconds, 77 a day, the one
build included.

### 2026-09-30 — a finding this did not fix

Re-invoking `make backfill` over a range that contains a day with a parked delivery fails. On
2026-08-20 the settlement run ended `failed`, correctly, because its delivery was parked; the loop
therefore treats the day as incomplete, tries to re-run it, and the tick guard refuses, the
simulation being past it. Specification 005 criterion 20's "re-invoking it over an
already-completed range changes nothing" holds for ranges without a parked day. It predates this
specification; the resolution, a day complete when every failed feed batch is a parked one,
belongs with the loop's owner.
