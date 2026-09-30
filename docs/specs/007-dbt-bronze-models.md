# 007 — dbt Project and Bronze Models

Status: Draft
Depends on: 000–006

## Goal
Stand up the dbt project and expose every landed entity as a bronze model: a
view over the lake that reads only batches the platform registered. Publish
the project's documentation to GitHub Pages, and run a `ci`-scale ingestion and
dbt build in CI so that bronze is exercised on every pull request rather than
only in a local acceptance run.

## Context
The `dbt/` tree is a skeleton of README files. Nothing reads the lake except
the ingestion code and the acceptance scripts. Specifications 005 and 006 land
50 entities as Parquet under `bronze/<source>/<entity>/ingest_date=/batch_id=/`,
register each batch in `ops.batch_registry`, and leave objects of failed or
interrupted runs in the bucket by design. ADR 0008 and `architecture.md` make
the registered-batch filter a rule: reading the object store without it reads
the output of runs that died.

Three constraints are inherited and are not re-decided here:
- DuckDB is single-writer, so every warehouse access goes through the
  `warehouse_access` pool, dbt included.
- The contracts in `contracts/` are the one source for what bronze carries:
  columns, types, primary keys and classifications. `docs/data_dictionary.md`
  is the one source for what the relational source is.
- The CI anchor moves to `ACCEPTANCE_ANCHOR` here (ADR 0014, consequence
  recorded in ADR 0016). The committed `ci` manifest regenerates once.

## Scope

### 1. The dbt project
- `dbt-core` and `dbt-duckdb`, pinned in the lockfile.
- **One DuckDB version.** The version dbt uses must be the version ingestion
  uses, pinned once. A warehouse file written by one DuckDB version and opened
  by another is a storage-format question to measure, not assume.
- **No dbt packages.** `dbt deps` fetches from the network, and every test this
  specification needs is small enough to write as a macro in the repository.
- `dbt/profiles.yml` is committed and reads everything from environment
  variables. MinIO credentials reach DuckDB only through the dbt-duckdb profile's
  secrets configuration, never through a hook, a model or a macro. A hook's
  rendered SQL is stored in `manifest.json`, which is published.
- `httpfs` and any other DuckDB extension are installed at image build. Nothing
  downloads an extension at run time.
- Targets: `ci`, `dev`, `full`, differing only in variables, per
  `architecture.md`.

### 2. Bronze models
- One model per contract entity, named `br_<source>__<entity>`, where `<source>`
  is the lake prefix and `_source_system` value for that entity. Where
  `model_inventory.md` names a bronze model differently, the lake prefix wins
  and the inventory is corrected.
- Materialised as views in the `bronze` schema, over the entity's lake prefix.
- **The registered-batch filter is the only way a bronze model reads the lake.**
  One macro reads the entity's objects and keeps only rows whose batch is
  registered in `ops.batch_registry`. No bronze model reads the lake any other
  way, and a check fails the build if one does.
- **Schema across contract versions.** Files written under different contract
  versions carry different column sets: `payments` widened a column, and
  `settlements` lost `merchant_name` from 2026-09-03. Files are combined by
  column name, never by position.
  - A bronze model exposes the union of the columns of every contract version
    of its entity.
  - A column retired by a later version stays in the model, null for rows
    landed after its retirement, and its description says from when.
  - Every column is cast to its type in the current contract version. Where a
    retired column has no current type, it takes the type of the last version
    that carried it.
- **An entity with no registered batch** yields an empty, correctly typed
  relation, and `dbt build` passes. The FRED feed without a key is the case
  that exists today.
- The Hive partition values in the object key must agree with the columns in
  the file: `batch_id` with `_batch_id`, and `ingest_date` with the registry. A
  test asserts it.
- API and file entities expose `_raw_payload`.

### 3. Generated, not hand-written
The dbt YAML for bronze models is generated from the contracts:
- column names, types and descriptions;
- each column's classification, carried in the column's `meta`, so the
  published documentation shows it;
- the declared grain;
- the tests below.

If the model SQL is uniform, it is generated too. Generated files are
committed, so a reviewer can read them, and `make dbt-generate CHECK=1` fails
in CI if any differs from what the generator produces. This is the
`schema-check` pattern: one source, one mechanical check.

The bronze section of `model_inventory.md` is generated between markers by
the same generator and checked the same way.

### 4. Tests on every bronze model
- **Grain:** the entity's primary key plus `_batch_id`, `unique` and
  `not_null`, severity `error`. Bronze holds one row per key per batch, and the
  same key recurs across batches by design: reference entities re-land their
  book every run, and incremental entities re-land changed rows.
- **Landing reconciliation:** the model's row count per batch equals
  `records_landed` in the registry for every registered batch, severity
  `error`. This is the test that proves the filter keeps exactly the
  registered rows.
- **Tokens:** every column classified `identifier` holds values in token
  format, severity `error`. It is a leak check at the model level, beside the
  object-level PII scan.
- **Audit columns** `not_null`.
- **No `store_failures`** on any column classified `sensitive`,
  `quasi-identifier` or `identifier`. A stored failure copies values into a new
  table.
- Every generic test is a macro in `dbt/macros/`, with a unit or dbt test that
  makes it fail.

### 5. Sources and lineage
`ops.batch_registry` is a dbt source, so lineage runs from the registry to
every bronze model. `meta.pii_vault` is never declared as a source and never
read by dbt. Declare any other `ops`, `dq` or `meta` table as a source only if
a model or test reads it.

### 6. Orchestration
`transform_bronze` runs `dbt build` over the bronze selection in the
`warehouse_access` pool.
- It is scheduled on the ingestion assets, with a condition that fires on any
  of them rather than all of them, and `max_active_runs=1`.
- Measure how many runs a 61-day backfill triggers and how much it slows the
  backfill. Report both before deciding whether the backfill loop should drive
  it instead.
- Where dbt runs inside the Airflow image, measure dependency conflicts
  between dbt and Airflow. If there are any, dbt gets its own virtual
  environment in the image and runs as a subprocess.

### 7. CI
The `stack` workflow seeds at `ACCEPTANCE_ANCHOR`, ingests a short window
through the real DAGs, and runs `dbt build`.
- **The window is chosen for coverage, not length.** 2026-07-20 to 2026-07-26
  is expected to include: the initial load, a weekend with no FX publication, a
  no-arrival settlement day, a late file, a correction, quarantined records and
  a sanctions snapshot. Measure that it does, and state what it does not
  include: the drift events, parking, and the FRED feed.
- **No live external call in CI.** Frankfurter responses for the window are
  recorded once, committed, and served through the feed's transport. The FX
  rates are public and non-personal.
- **Measure the time per ingested day in CI** and where it goes. Report
  before choosing the window length. The `stack` check's total time is
  reported.
- The anchor move regenerates the committed `ci` manifest. Its commit body
  states what changed in the generator's inputs, why the data moved, and which
  tables' hashes shifted.
- The four required checks stay the four required checks.

### 8. Documentation on GitHub Pages
- On every push to `main`, a workflow builds the `ci` warehouse as in section
  7, runs `dbt docs generate --static`, and deploys the result to GitHub Pages.
- **Before deploying, it scans the whole artefact** for every secret the
  environment holds and for every vault value. It fails on a hit, and it is
  proven able to fail by a planted value.
- The published site shows each column's classification.

### 9. Documents
- ADR: bronze as registry-filtered views over the lake. It must name the
  rejected alternatives with the reasons they lost:
  - materialising bronze tables in DuckDB, which would be a second copy of the
    lake;
  - reading the lake unfiltered.
  It must also name the negative consequence: every read lists every object
  under the entity's prefix, with the cost stated at `ci` and `dev` and
  projected for `full`.
- `architecture.md`:
  - replace the `ci` profile's "a full pipeline run completes in under a
    minute" with the measured figures;
  - correct the macro indicators load pattern in the sources table, which is a
    full re-request with append, not an incremental append;
  - describe the bronze view and its filter.
- `docs/runbook.md`: running dbt locally and in the stack; reading the
  published docs.
- `project_state.md` at the close. This specification closes M4, so the M4
  checkpoint is written with it.

## Out of scope
Silver and every model above bronze. Quarantine models. The `dq` framework and
freshness checks. The tie-cluster cursor for reference extraction. Compaction
and retention. The BigQuery target: a view over S3 Parquet is DuckDB-specific,
and M10 decides how bronze is expressed there.

## Acceptance criteria
1. `dbt build` over the bronze selection passes on the `ci` warehouse, locally
   and in CI, with zero warnings treated as errors ignored.
2. The number of bronze models equals the number of contract entities: 50,
   confirmed at planning and stated as a number in the test.
3. Every bronze model reads the lake only through the registered-batch macro,
   enforced by a check that is itself proven able to fail.
4. An object planted under an entity's prefix with an unregistered batch id,
   and the objects of a failed batch, are absent from the model.
5. For every registered batch, model rows equal the registry's
   `records_landed`, on the full local acceptance history and in CI.
6. Files from different contract versions combine by name. Rows landed before
   and after the settlements `merchant_name` retirement both appear, with the
   column populated before and null after, on the local acceptance history.
7. An entity with no registered batch yields an empty relation of the right
   columns and types, and the build passes.
8. The grain test, the token test, the audit-column tests and the Hive-key
   consistency test pass on every model. Each generic test is proven able to
   fail.
9. `make dbt-generate CHECK=1` passes, and fails when a generated file is
   edited by hand or a contract changes without regeneration.
10. No hook, model or macro contains a credential. The published artefact scan
    finds no secret and no vault value, and catches a planted one.
11. dbt's DuckDB version equals ingestion's, pinned once.
12. The unit and DAG suites, including any new ones, pass with networking
    disabled, and no extension is downloaded at run time.
13. `transform_bronze` runs in the `warehouse_access` pool and fires on any
    ingestion asset. The backfill's run count and slowdown are reported.
14. CI ingests the stated window with no live external call and runs
    `dbt build`. What the window exercises and what it does not are reported.
    The `stack` check's duration is reported.
15. The committed `ci` manifest is regenerated at `ACCEPTANCE_ANCHOR` in one
    commit whose body follows the manifest rule.
16. The documentation is published to GitHub Pages from `main`, and shows every
    column's classification.
17. The ADR, the `architecture.md` corrections, the runbook and
    `model_inventory.md`'s generated bronze section are in place.
18. The M4 checkpoint is written and `project_state.md` reflects M4 closed.
19. All four checks pass; delivered as a pull request on `feat/M4-dbt-bronze`.
