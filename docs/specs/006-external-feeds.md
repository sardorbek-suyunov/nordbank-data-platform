# 006 — External Feeds

Status: Approved
Depends on: 000–005

## Goal
Land the four non-relational sources through the framework specification 005
established, adding the two ingestion modes it does not have: a versioned
full-refresh snapshot, and file arrival. This is where quarantine, schema drift
in a payload, and reconciliation against an external party become real rather
than injected.

## Scope

### 1. Ingestion modes to add
- **Snapshot** — a full publication replaces the prior one, versioned by its
  publication date. No watermark. Re-landing the same version is a no-op; a new
  version is a new batch.
- **File arrival** — files land under a lake prefix and are ingested by
  identity, not by interval. `ops.ingested_file` keyed on **content checksum**,
  so the same file reprocessed never double-lands, and a file renamed but
  unchanged is recognised.
- **Interval API** — a request per date or date range, retried with backoff.

Incremental-by-watermark from 005 is unchanged.

### 2. The four feeds

**ECB FX rates, Frankfurter API, interval.** One request per date in the
window. The ECB publishes on working days only, so weekends and holidays are
**absent dates in bronze**, not filled rows — carrying the last published rate
forward is silver's job and doing it at ingest would destroy the evidence that
the gap existed. Land the response payload alongside the parsed rows.

**Card network settlement files, file arrival.** Generated from
`core.transactions` plus the scheme's settlement lag by a generator extension,
written as CSV to the lake, and then ingested as if a third party had sent them.
The generator is simulation infrastructure, on the far side of the boundary M3
established, and the ingestion path knows nothing about how the file was made.

The file generator produces, at configurable rates stated in
`generator_realism.md`:
- **Malformed records** — an unparseable amount, an invalid date, a missing
  required field, a row with the wrong field count. These are the genuine
  quarantine traffic the relational source structurally cannot produce.
- **Late files** — a file for settlement date D−3 arriving today.
- **Schema drift** — one file with an extra column, one with a changed column
  type, on a scripted timeline.
- **Settlement breaks** — a deliberate discrepancy between the file total and
  the ledger, at a rate and magnitude that exercises both severities in
  `metric_definitions.md`.

**Sanctions and PEP list, snapshot.** The OpenSanctions consolidated list,
downloaded whole, stored under a version derived from its publication date. The
synthetic fixture from M3 is merged into the local snapshot so screening has
something to match. **No real sanctioned individual's name appears in the
repository or in any committed fixture**, and the reason is restated here.

**FRED macro series, interval, optional.** Monthly. Requires a free key, so the
DAG **skips with a stated reason** when `FRED_API_KEY` is absent rather than
failing, and nothing in CI depends on it. Revisions to published periods are
expected: land them as new batches and let silver keep the history.

### 3. Late files and the partition
A file for settlement date D−3 lands in **today's** ingest partition, with its
settlement date as a business attribute. It does not reprocess D−3's partition —
bronze partitions by ingest date and never rewrites, per ADR 0008. What is
rebuilt for the affected settlement dates is the reconciliation mart, at M6.

`architecture.md` currently says late files "reprocess the partition of their
settlement date". That predates ADR 0008 and is now wrong. Correct it.

### 4. Payload fidelity
API and file sources carry `_raw_payload`: the record as received,
**structurally faithful with identifier fields replaced by their tokens**, per
ADR 0005. For a settlement file that means the cardholder reference is
tokenised in the payload as well as in the parsed columns, using the same keyed
hash as the relational source — so a card identifier resolves to the same token
whichever feed it arrived through. That cross-source determinism is the point
and it must be proven, not assumed.

### 5. Contracts for non-relational sources
Contracts extend to cover a file's expected header and field order, and an API
response's expected shape. Bootstrapping from the data dictionary does not apply
here — there is no dictionary entry for a third party's file — so these
contracts are authored, with their source of truth named in the contract itself
(a published API reference, a file specification).

**Contract-of-the-time selection.** The contract in force for a batch is
selected from `meta.contract_version` by the batch's interval, not by what is on
disk. This closes the reproducibility gap 005 left: replaying history no longer
requires checking out an old contract by hand.

### 6. External dependencies in tests
No test may require network access. Record real responses once, commit them as
fixtures, and run the suite against them. Prove it by running the full suite
with networking disabled. Live calls happen only in local or manual runs, and
the recorded fixtures carry the date and endpoint they were captured from.

State the trade-off: a recorded fixture ages, and an API that changes shape will
be detected by a live run rather than by CI. Name what triggers a re-record.

### 7. Failure handling
Per request: bounded retry with exponential backoff and jitter, a request
timeout, and a cap on total attempts. A rate limit response is retried, not
failed. Whatever the outcome, a batch is registered only when complete — a
partially fetched interval leaves the batch `written` and the watermark unmoved,
exactly as in 005.

### 8. Make targets and orchestration
DAGs: `ingest_fx_rates`, `ingest_card_settlements`, `ingest_sanctions_list`,
`ingest_macro_series`. `schedule=None`, driven explicitly, for the reason
recorded in 005. Assets per feed. The settlement DAG waits on file arrival with
a deferrable sensor, which is the first thing in this platform that genuinely
waits on something external.

`make generate-settlement-files DATE=...`, and the backfill loop extended to
generate files, then ingest reference, core, and the feeds in an order it states.

### 9. Structural test rule
Every DAG and structural test asserts a minimum cardinality — task count, asset
count, non-empty mapped expansion input. A test that passes against an empty DAG
is not a test. Apply it retroactively to the tests specification 005 delivered.

## Out of scope
Silver, dbt, FX gap-filling, sanctions matching, the reconciliation mart.

## Acceptance criteria
1. Snapshot mode lands a versioned publication; re-landing the same version is a
   no-op, and a new version lands as a new batch. Prove both.
2. File ingestion is idempotent by checksum: the same file reprocessed does not
   double-land, and a renamed unchanged file is recognised as already ingested.
3. A late file lands in the current ingest partition with its settlement date as
   a business attribute. Report the partition and the attribute.
4. Malformed file records quarantine individually with their reasons, and
   landed plus quarantined equals records read — **non-trivially, with a
   quarantined count above zero**, in contrast to criterion 14 of 005.
5. A file with an extra column lands and logs additive drift; a file with a
   changed column type quarantines the whole file and fails its batch.
6. FX rates land for every publication date in the window, and weekend and
   holiday gaps are absent rather than filled. Report the count of absent dates
   and confirm none was fabricated.
7. Rate limit, timeout and server error are each retried with backoff and none
   registers a partial batch. Demonstrate all three.
8. The full test suite passes with networking disabled.
9. `ingest_macro_series` skips with a stated reason when the key is absent, and
   nothing in CI depends on it.
10. Settlement file totals reconcile against the ledger per settlement date and
    currency at source precision, with the injected breaks detected at the
    severity `metric_definitions.md` specifies.
11. A card identifier arriving through a settlement file resolves to the same
    token as the same identifier arriving through the relational source.
12. `_raw_payload` is present for every API and file record, structurally
    faithful, with no cleartext identifier. Prove by the byte-wise scan from 005.
13. The sanctions snapshot is versioned by publication date and the synthetic
    fixture is present and matchable.
14. No real sanctioned individual's name appears in the repository, in any
    fixture, or in any landed object.
15. Contract-of-the-time selection works: replay a batch from before a contract
    version bump without editing any file on disk.
16. Assets are emitted per feed and visible in Airflow.
17. The extended backfill runs all feeds in a stated order over the window, and
    every batch ends `registered` or explicitly `failed`.
18. Every structural test asserts a minimum cardinality, including those
    delivered by 005.
19. All four checks pass; delivered as a pull request on `feat/M4-external-feeds`
    with all four green.

## Amendments

Appended during implementation. The scope text above is left as issued; the protocol is in
`docs/specs/README.md`.

### 2026-09-23 — contract dates are authored against one pinned anchor

Section 5's selection, as ruled before implementation, gives every contract an authored
source-time `in_force_from`. The simulated source fires its scripted drift at an offset from the
seed anchor (`generator/drift/timeline.py`), so the business day on which `payments` widens is
`anchor + 37` and differs for every anchor, while an authored date is absolute. An authored date
is therefore correct for exactly one anchor.

The committed contracts are authored against `ACCEPTANCE_ANCHOR`, 2026-07-20, named once in the
timeline module, and a unit test asserts that every contract version accepting a scripted event
takes over on the day that event fires at that anchor. The acceptance procedure seeds at that
anchor rather than at the runbook's `today − window` example. That still meets the constraint
the runbook states — the window must end on or before today, and 2026-07-20 plus sixty days is
2026-09-18 — and it is the anchor specification 005's acceptance run used, so the two
specifications' evidence describes one history. ADR 0014 records the decision and the
restriction it places on exercising the repository at other anchors.

### 2026-09-23 — rulings made before implementation

The review of this specification before implementation ruled on more than five points, which
under the reissue protocol in `docs/specs/README.md` calls for a version 2 with a changelog. The
rulings came with the instruction to proceed, and reissuing the specification is its author's
decision, so they are indexed here in the meantime; the implementation follows the rulings
rather than the text above wherever the two differ, and each has its own record elsewhere.

- **The sanctions list is synthetic in content and real in shape** (ADR 0015). Section 2's
  "downloaded whole" and criterion 14's "in any landed object" could not both hold: the real
  list held 300,971 entities when measured. The version is the publisher's own string, not a
  date derived from publication, because the publisher exports four times a day under a new
  string each time; a snapshot is identified by its content checksum. Criterion 13's
  "versioned by publication date" is met as "versioned by the publisher's version, with its
  export timestamp recorded".
- **Breaking file drift is a header change, not a type change.** A CSV carries no types, so
  criterion 5's "changed column type" was observable only in values, where it collides with
  criterion 4's record-level quarantine. The scripted breaking event removes `merchant_name`,
  which also gives specification 005's unit-test-only "removed column" kind its first end-to-end
  proof. Criterion 5 is met with a removed column.
- **The clearing file carries a trailer**, the processor's own record count and amount total, so
  that malformed records and settlement breaks are independently settable. Criterion 10
  reconciles the trailer against the ledger; criterion 4 reconciles records within the file.
- **It is an issuer clearing file**, carrying the issuer's `card_reference` beside the masked
  number, which is what makes criterion 11 a property of a tokenised value.
- **The deferrable sensor stays, with a run that shows it waiting**, and running out of time is
  an answer — empty batches for the day — rather than a failure.
- `mart_control_settlement_reconciliation` is built at M6, not M7; section 3 is right and
  `model_inventory.md` was corrected.

### 2026-09-23 — a partially fetched interval is failed and writes nothing

Section 7 says a partially fetched interval "leaves the batch `written` and the watermark
unmoved, exactly as in 005". In 005 `written` is the state between the extract phase writing
objects and the register step registering them. An interval feed does better by not writing
until every request of the interval has an answer: a partial interval writes nothing, and the
register step marks its batch `failed` with the dates that did not answer. The watermark is
unmoved either way, and the next run requests the whole interval again, which `make fault-demo`
shows. The dates that did answer are recorded in `ops.feed_request` as `discarded`, not
`landed`, because nothing of them was written.

### 2026-09-23 — one batch per delivered file, keyed on its settlement date

Section 1 says files are ingested "by identity, not by interval". Identity decides whether a file
lands; a batch still needs a key. A clearing file lands as two batches, one per entity, keyed on
its settlement date, and in the ingest partition of the day it arrived: a late file for D−3 is
`settlements-<D−3>-NN` under `ingest_date=<D>`. A day on which the processor sent nothing for
its own settlement date registers empty batches keyed on that day, so a file for it arriving
late lands as the next sequence. A snapshot's batch is keyed on the publisher's export time, the
first interval in the platform with a non-zero time of day, which is what specification 005
kept the six trailing digits of the batch id for.

### 2026-09-23 — the gate also requires the open step's allocation

Found while building the feed DAGs. With register on `all_done` and the gate as the only leaf
task, a run whose open step fails leaves nothing allocated, the register step registers nothing,
the gate reads a summary with no failures, and the run ends green. The feed DAGs' gate takes the
open step's output as well and fails when there is none. Specification 005's two DAGs have the
same shape and the same gap; it is fixed there in its own commit, with the measurement.

### 2026-09-23 — four measurements the implementation depended on

- **Airflow's plugin loader registers every file under the plugins folder by its bare name.**
  `nordbank_ops/feeds/http.py` became `sys.modules['http']` in every Airflow process and the
  scheduler, API server and DAG processor all failed at start-up. The module is `fetch.py`, and a
  test refuses any plugin module named like a standard-library one.
- **A decorated mapped task's expansion input is `op_kwargs_expand_input`**; `expand_input` is an
  empty placeholder, and section 9's non-empty assertion written against it reported correctly
  wired DAGs as mapped over nothing.
- **Pulling a mapped task's XCom returns one value per map index when there are several, and the
  single value when there is one**, so the register step flattens either shape.
- **A cleared run keeps the DAG version it was created with**, and there is no CLI option to
  clear onto the latest one. Measured while fixing the gate: clearing the run that exposed the
  gap re-ran it with the old gate. A contract change still reaches a cleared run, because
  contracts are read from disk at run time, which is what the backfill's resume after a
  contract bump relies on; a code change reaches only new runs.
- **Airflow's processes keep plugin code they have already imported until they restart.** The
  gate change produced no new DAG version until the scheduler, DAG processor, triggerer and API
  server were restarted, after which `ingest_core_banking` and `ingest_reference_data` moved to
  version 2. The acceptance evidence is therefore taken from a run started on a restarted stack
  with the final code, not from the run during which fixes were made.
- **A mapped task expanded to nothing still returns its earlier try's XCom.** On a cleared
  settlement day whose file had already landed, the extract task had no instances this time,
  its earlier instance was marked removed, and pulling its XCom returned the earlier report;
  the register step tried to register a batch that was already registered and the registry
  refused. The register step now keeps only the reports for batches this run's open step
  allocated.
- **The full test suite is the unit and DAG suites.** Criterion 8's "with networking disabled"
  is met for those by `make test-offline`, which runs them with `--network none`. The integration
  suite exists to reach the stack's services and cannot run without a network; every suite
  installs a guard that refuses public addresses, which is what proves the integration suite
  reaches the stack and nothing beyond it.
