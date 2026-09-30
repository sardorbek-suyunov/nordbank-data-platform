# M4 — Ingestion into bronze

Specifications: [005-extraction-and-bronze-landing.md](../specs/005-extraction-and-bronze-landing.md),
[006-external-feeds.md](../specs/006-external-feeds.md),
[007-dbt-bronze-models.md](../specs/007-dbt-bronze-models.md)
Status: complete
Date: 2026-09-30

## What was built

Every source the platform has lands in the lake, is registered, and is readable as bronze.

**Extraction and landing (005).** Two DAGs extract the forty-five `core` and `ref` entities by
watermark with an overlap, in four phases that keep the single-writer warehouse out of the bulk
work: a pooled open step allocates a batch per entity, mapped extract tasks read, validate
against the contract, tokenise identifiers and write Parquet, a pooled register step registers
what wrote and fails what did not, and a gate fails the run. The batch id is in every object key
(ADR 0008). Identifiers are tokenised before anything is written; the register step fills the
vault by re-reading the source, so cleartext never crosses a task boundary (ADR 0005). Contracts
are bootstrapped from the data dictionary once and hand-versioned after, with `contracts-diff` in
CI. A backfill loop interleaves the source's tick with the day's ingestion.

**External feeds (006).** ECB rates through Frankfurter by interval, the card processor's clearing
files by arrival with a deferrable sensor, the sanctions list as a snapshot, and FRED by interval,
skipped without a key. Deliveries are identified by the checksum of their bytes (ADR 0013), read
against the contract in force on the day the sender produced them (ADR 0016), and parked when a
verdict refuses them. The processor and publisher are simulations on the far side of an inbound
bucket.

**Bronze (007).** A dbt project with one target, run from its own environment in the image. Each
of the fifty contract entities is a generated view that reads its lake prefix by column name,
casts to the contract, and keeps only rows whose object key's batch is registered (ADR 0018).
Every model carries the grain, landing reconciliation, token, object-key and audit tests, each
shown failing on a planted fixture. `transform_bronze` builds and tests bronze after every
registration. The stack job ingests a week through the real DAGs, with no live external call,
builds bronze, and publishes the documentation site from `main` after scanning it.

## Measured

| | Figure |
|---|---|
| Entities | 50: 45 `corebank`, 2 `cardnet`, 1 `ecb`, 1 `opensanctions`, 1 `fred` |
| Sixty-one-day acceptance history at `ci` | 2,971 batches, 325,899 rows landed, 118 quarantined, 20,015 vault values |
| Bronze models and tests | 50 views, 267 tests, 318 nodes passing with `--warn-error` and no warning |
| A day ingested, locally | 76 seconds with `transform_bronze` building after every registration |
| A day ingested, GitHub runner | 77 seconds, the one bronze build at the end included |
| `dbt build` over bronze | 9 to 12 seconds over sixty-one days locally, 14 seconds after a week in CI |
| The `stack` job | 14 minutes 32 seconds, of a 30-minute limit |
| Reading all of bronze at `ci` | 2.46 seconds and 3,017 requests over 2,968 objects (ADR 0018) |
| Unit, DAG and offline suites | 569 unit tests, 41 DAG tests, 612 passing with `--network none` |

## What the measurements changed

Specification 007 was reissued at version 2 before anything was built, because planning measured
more than five corrections. Those that changed the design:

- **The writer inferred Parquet types from values.** 13 of 49 entities carried two to seven
  physical schemas; only settlements because of a contract version. The writer now writes each
  batch in its contract version's schema, and `make ingest-integrity` checks that no entity has
  more schemas than versions: 12 entities failed that check on the fourth acceptance run, none on
  the acceptance history written since.
- **DuckDB needs `union_by_name`, and a filter on the file's `_batch_id` is not a filter.** Without
  the first, four of the thirteen entities failed to read; with a filter on the second, a planted
  copy of a registered file passed. The views read by name and filter on the object key.
- **A list of asset aliases never fires while one feed is silent.** `transform_bronze` is scheduled
  on the six aliases joined with `|`.
- **dbt does not resolve under Airflow's constraints**, so it has its own environment, and DuckDB
  is pinned once, in the lock, because an extension is built for one DuckDB version.

The build found five more, each recorded as an amendment to specification 007: dbt 1.12 reads the
repository's `.env`; the generator's integration tests still seeded at the old anchor;
`transform_bronze` and the backfill loop's registry reads collided over the warehouse file; the
scan's copy of `.env` outlived the scan; and the stack job came within ninety seconds of its
timeout until the loop left `docker compose exec`.

## `transform_bronze` against the backfill

Specification 007 fixed the rule before the measurement: if building bronze after every
registration slows the sixty-one-day backfill by more than 15 per cent of wall time, the backfill
pauses `transform_bronze` and builds once at the end. Two runs on one commit, a throwaway stack
seeded at the acceptance anchor, 2026-07-20 to 2026-09-18:

| | Active | Paused |
|---|---|---|
| Wall time | 4,649 s, 76 s a day | 3,600 s, 59 s a day |
| Builds | 243 from 253 registering runs, all successful | none |
| A build | median 9.2 s, at most 12.1 s | |
| Pooled tasks' wait for the slot | median 1.9 s, 23.9 minutes in all | |

29.1 per cent, so the backfill now builds once at the end, which over the whole history took 31
seconds. Daily operation keeps the schedule. The active run showed it firing: a build on the empty
warehouse made fifty typed empty views, and after the first day's registrations the schedule
rebuilt the forty-nine whose entities had landed rows; FRED stayed empty.

## Acceptance criteria, specification 007

| # | Criterion | Evidence |
|---|---|---|
| 1 | `dbt build --warn-error` passes on the `ci` warehouse, locally and in CI, nothing silenced | 318 of 318, no warning: locally on the sixty-one-day history and in every green `stack` run. A dbt deprecation would fail it; none fires |
| 2 | 50 bronze models, stated as a number in the test | `test_dbt_generate.py` states 50 and compares the committed models with the contracts |
| 3 | Only through the macro, enforced by a check proven able to fail | `bronze_guard` fails the build; `make dbt-prove` plants a bronze model reading the lake and a silver one, and both fail it |
| 4 | A planted unregistered object and a planted copy of a registered file are absent | `make bronze-plant`: 129 rows each, in the lake and absent from the model, on the throwaway stack's full history and in CI; `dbt-prove` shows the same on fixtures |
| 5 | Model rows equal `rows_landed` for every registered batch, full local history and CI | The reconciliation test passes on both; the fifty views hold 325,899 rows, the registry's registered total |
| 6 | All 13 multi-schema entities read; `merchant_name` populated before and null after, by delivery date | All thirteen equal the registry on the fourth run's lake. Settlements: 7,984 rows delivered before 2026-09-03, 7,290 with a name; 3,276 from it, none; the late file for 2026-09-01 sent on 2026-09-04, 156 rows, none |
| 7 | An entity with no registered rows builds typed and empty | FRED, on every build; all fifty on an empty warehouse, each `WHERE false` with the contract's types; `dbt-prove`'s empty fixture |
| 8 | Grain, reconciliation, token and Hive-key tests pass everywhere and fail on fixtures | 267 tests pass; `make dbt-prove` shows each of the six failing on its planted entity and passing on the clean one |
| 9 | `dbt-generate CHECK=1` passes, fails on a hand edit or an unregenerated contract | In the `docs` job; unit tests show a hand edit, a contract change and a stray file each failing it |
| 10 | No credential in a hook, model or macro; the artefact scan is clean and catches a plant | Credentials only in the profile's secrets block as `DBT_ENV_SECRET_` variables; a planning probe put the same value through a hook and found it in the manifest. `make docs-scan`: 13 secrets and 20,015 vault values, no hit, 811 of 811 classifications; `PLANT=1` catches both |
| 11 | DuckDB pinned once; the image's exports agree with the lock, checked in CI | The `warehouse` group; `make requirements CHECK=1` in the `docs` job, failing on a hand edit; a test holds `pyproject.toml` to one declaration |
| 12 | httpfs loads with `--network none` from the build, automatic install off | Two tests in the image under `make test-offline`: install and load with no network, and dbt's DuckDB is Airflow's |
| 13 | `transform_bronze` in the pool on the six aliases joined with `|`; runs, builds, slowdown, rule | DAG tests assert the `AssetAny` of every ingestion DAG's alias and the pool. Above: 253 registering runs, 243 builds, 29.1 per cent, rule applied |
| 14 | CI ingests the window with no live call and runs `dbt build`; coverage and duration reported | FX from recordings, live URL unroutable. Covered: the initial load, an FX weekend, two no-arrival days, a late file, a correction, 20 quarantined records, a sanctions snapshot. Not covered: drift, a second contract version, parking, a failed batch, FRED, a second snapshot. The `stack` job: 14 minutes 32 seconds; the week 543 seconds, 77 a day, the one build at the end included |
| 15 | The `ci` manifest regenerated at the anchor in one commit following the rule | `chore: regenerate the ci manifest at the acceptance anchor`: the anchor the only input changed, all sixteen digests moved, eight row counts, 163,281 to 163,899 |
| 16 | Published to Pages from `main`, every column's classification shown | The deploy job runs on a push to `main` only, after `stack`; the scanned site shows 811 of 811. Published at the merge |
| 17 | The writer writes the contract's schema; both integrity checks pass and are proven able to fail | 49 schemas over 49 entities for a week, 50 over sixty-one days with settlements' two versions; unit tests fail each check on a plant; the fourth run's lake fails check 7 on 12 entities |
| 18 | ADR, document corrections, the generated inventory section | ADR 0018; `architecture.md`'s bronze view, the macro indicators pattern and the measured `ci` profile; the runbook; the inventory's generated section with `br_cardnet__settlement_totals` |
| 19 | The M4 checkpoint, and `project_state.md` shows M4 closed | This document |
| 20 | All four checks pass; a pull request on `feat/M4-dbt-bronze` | `lint`, `dags`, `docs` and `stack` green; pull request [sardorbek-suyunov/nordbank-data-platform#8](https://github.com/sardorbek-suyunov/nordbank-data-platform/pull/8) |

## Deviations from the specifications

Each is recorded where it belongs: specifications 005 and 006 carry their amendments, and 007's
are appended to it. None changes what a criterion asks for.

## Verification

```bash
make up && make schema-apply && make warehouse-apply
NORDBANK_ENV=ci NORDBANK_SEED=42 NORDBANK_ANCHOR_DATE=2026-07-20 make seed
make backfill FROM=2026-07-20 TO=2026-09-18
make ingest-integrity && make bronze-pii-scan
make dbt-build
make bronze-plant ACTION=plant && make dbt-build && make bronze-plant ACTION=verify
make dbt-docs && make docs-scan && make docs-scan PLANT=1
make dbt-generate CHECK=1 && make dbt-prove && make requirements CHECK=1
make test && make test-dags && make test-offline
```

## What M5 inherits

**Bronze is a view, and a read costs a listing.** Silver reads bronze, so every silver build lists
every prefix and reads every footer. At `ci` that is seconds; at `full` it is what compaction, M7's,
has to bound (ADR 0018).

**Bronze is not deduplicated.** The grain is the key plus `_batch_id`, and reference data re-lands
its whole book on every run: silver deduplicates on the business key and `updated_at`, and must
expect a `merchants` version whose every contract column equals its predecessor's.

**A retired column is null by delivery date.** `merchant_name` is null for clearing files sent from
2026-09-03, including a late file for 2026-09-01; silver joins the merchant through
`transaction_reference` instead.

**FRED has never landed.** Its view is typed and empty, and becomes real at the first build after
a keyed run registers.
