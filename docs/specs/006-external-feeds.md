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

### 2026-09-25 — the review of the implementation, and the rulings on it

The implementation was reviewed with 27 numbered points and ruled on in two phases. The rulings
are recorded where the decision lives — ADR 0016, `architecture.md`, `conventions.md`, the
contracts and `generator_realism.md` — and indexed here, because each departs from the text
above or from an earlier amendment. Specification 006 is to be reissued at version 2 by its
author; nothing above is edited.

- **Contract selection is by the day the sender produced the delivery** (ADR 0016, superseding
  ADR 0014's selection). Section 5's "by the batch's interval" holds for the relational source,
  the API feeds and the snapshot, whose intervals start on that day, and not for a delivered
  file, whose batch is keyed on the settlement date it covers. A file is now read against the
  contract in force on its ingest date. Measured before the ruling: a late file for 2026-09-01
  sent on 2026-09-04 in the new layout failed on every run with no way out. The comparison is
  `in_force_from` on or before that day, date only.
- **A delivery refused with a verdict is parked**, and attempted again only when the contracts
  in force for it change. The parked state is derived from the sightings, the registry and the
  recorded contract versions, not stored.
- **The clearing file's header declares a revision**, and the header's `created_at` lands as
  `file_created_at`, because `created_at` is reserved for audit columns. Contract versions 1
  and 2 of the clearing file were edited in place to add both, before any warehouse outside the
  branch recorded them; the third acceptance run is the first to hold them.
- **The batch arithmetic is stated per ingestion mode** in `architecture.md`. For files,
  additivity is over the sender's `file_sequence`, never the registry's `batch_sequence`; within
  a sequence the highest declared revision replaces; the same settlement date, sequence and
  revision over different content fails loudly, as does a snapshot version string reused over
  different content.
- **The additive drift field is the acquirer reference number**, not an interchange amount. The
  interchange amount was a flat 0.2 per cent that contradicted `ref.interchange_rates` or
  had no counterpart in it on 2,804 of 6,967 rows in the second run, a second, contradicting source of
  interchange. The drift event's day is unchanged.
- **The generator builds each file in the layout of the day it is sent**, and scripts three
  cases so that every acceptance run exercises them: a late file that straddles the breaking
  change (settlement date anchor plus 43, sent anchor plus 46), a correction arriving with the
  late file it corrects (anchor plus 36), and a transmission cut off part way, re-sent complete
  and retried cut off (anchor plus 31). Corrections of break-carrying files are drawn at 0.25.
- **The trailer's per-cell counts are not a gate.** The end record is the truncation gate. A
  record damaged in transit cannot always be put in a cell: in the second run 14 quarantined
  records had a blank network or currency and 30 more the wrong field count, all in complete files. The settlement totals
  contract records why.
- **A day with no delivery carries `empty_reason = no_arrival_within_window`**, which a
  delivered empty file does not, so M7 freshness can alert on one and not the other.
- **An unpublished FX date is logged `absent_no_publication`** with the date the API answered
  with, distinct from `landed` and `discarded`; criterion 6 counts from it.
- **A failed batch keeps its read and quarantined counts**, and `make ingest-integrity` checks
  the registry's quarantined count against the quarantine index for every terminal batch.
- **The sanctions version is the simulated publisher's.** Where the amendment of 2026-09-23 says
  "the publisher's own string", the string is generated by the simulated publisher in the real
  publisher's form, `YYYYMMDD070000-xxx`, and copied from no real export; the one real export
  string the repository records, in the sanctions contract, is not among them.

### 2026-09-25 — criterion 12's byte-wise scan could not see column values

Criterion 12, and specification 005's criterion 8, were evidenced by a scan that searched the
bytes of every bronze and quarantine object for every vault value. Bronze is snappy-compressed
Parquet, and snappy replaces a run of bytes it has already seen with a back-reference, so a
value sharing a prefix with its neighbour does not appear contiguously. Measured at the review
over 400 card-reference-shaped values written with the platform's own writer: a byte-wise search
found none of them in the snappy object and all 400 in the uncompressed one. The "no hits"
reported by the first two acceptance runs therefore proved almost nothing about column values.
The scan now also decodes every value of every object and searches each vault value in every
encoding the objects use; `make bronze-pii-scan PLANT=1` plants a cleartext card reference in a
copy of a real object and shows the byte-wise reading miss it and the decoded reading catch it.
Re-run over the second run's lake, the decoded reading found no leak, and found ten vault values
in the sanctions snapshots: the M3 fixture's names, vaulted as payment counterparty names and
planted in the synthetic list on purpose. A value in a sanctions snapshot is excused only when
the simulated publisher's delivered file carries it too.

### 2026-09-25 — four defects found while implementing the rulings

- **Two deliveries for one settlement date in one run shared a batch.** The registry reuses an
  open batch for an interval, which is how a retry finds its batch; a late file and its
  correction arriving together would have been given one batch, and one would have overwritten
  the other's objects. A file now reuses only a sequence its own sightings were allocated.
- **A renamed copy seen in the same run as its delivery was a second attempt** at the same
  batch, which the register step would have refused. It is now sighted against the first.
- **The acceptance report imported the severity rule from the simulation**, which labels the
  breaks it injects with the same function, so "detected at the expected severity" agreed by
  construction. The report now implements the rule from `metric_definitions.md` itself.
- **The report dumped nothing.** Two figures of the first run could not be attributed once its
  warehouse was gone. `make feeds-acceptance RUN=name` now dumps the tables behind the report,
  searched for vault values before they are kept, and `scripts/compare_runs.py` compares two runs
  figure by figure.

### 2026-09-28 — the third acceptance run, and what it measured

The third run is the evidence of record: a fresh stack seeded at the acceptance anchor, every
contract committed, the backfill over 2026-07-20 to 2026-09-18. It was interrupted once, on
2026-09-25 during 2026-07-30's core banking run, when the stack was stopped; it resumed on
2026-09-28 from that day without a reseed, re-ran only that day's feeds, and finished with exit
0. The second run's identity demonstrations were repeated on it: a renamed copy of the
2026-09-10 file, a cleared re-run of 2026-09-18's settlement run, a sanctions run for a
non-Monday against an unchanged export and a cleared re-run of 2026-09-14's.

Against the second run, twenty of forty figures differ, and every difference is caused by a
change made in the review: nine corrections (eighteen batches, 1,574 rows read, nine quarantined
records drawn afresh in transit), the straddling late file (one more empty no-arrival batch, one
more late batch, three fewer sightings of the 2026-09-01 file, and its two quarantined records
in batch -02 with the new layout's field count), the cut-off transmission (four failed batches,
57 parked and 2 reattempted sightings, a resent file in place of the original key), the added
field's new name, and the FX request status's new name. No difference is unattributed; the
tables behind both runs are in `data/acceptance/run2/` and `data/acceptance/run3/`, and
`scripts/compare_runs.py` prints the comparison.

Two things were measured that the rulings did not anticipate.

- **A reattempted delivery can be refused for its second fault.** The cut-off transmission,
  parked on 2026-08-20 as structurally malformed, was attempted again on 2026-09-03 when the
  contracts in force for it changed, and was refused as a declaration conflict: its complete
  re-send had landed on 2026-08-21 under the same settlement date, sequence and revision, and
  the declaration is checked in the open step, before the file is read. The refusal is right
  and the file stays parked; the reason recorded is the less useful of the two.
- **A cleared run does not move cleanly onto the DAG's latest version.** The implementation's
  amendment above says a cleared run keeps the version it was created with. Measured on the
  stack after the third run, with `ops_stack_healthcheck`: after its DAG file changed,
  `airflow tasks clear` re-ran three of its four tasks on the new version and one on the old;
  the REST clear with `run_on_latest_version` re-ran all four on the new one. `make backfill`
  resumes with the CLI, and `docs/runbook.md` states the procedure after a code fix.

### 2026-09-29 — the rulings on the third run, and one finding

The third run's review was ruled on item by item; each change is its own commit, measured before
and after, and the fourth acceptance run is taken on the result.

- **Asset events only for registered batches.** Airflow emits an event for every `Asset` declared
  as a task outlet whenever the task succeeds, and the register step runs on `all_done` and
  succeeds having registered nothing: in the third run the 2026-08-20 settlement run failed both
  batches and emitted two events, runs that registered four or six batches emitted two, and
  cleared re-runs emitted again. The register step now declares one `AssetAlias` per DAG and
  adds one event per registered batch, carrying its batch id, rows landed and `empty_reason`.
  Specification 005's DAGs had the same shape; measured on a throwaway stack, an
  `ingest_core_banking` run whose sixteen batches all failed emitted sixteen events before the
  change and none after. Criterion 16's "assets emitted per feed" is met as one event per
  registered batch, and the assets appear in Airflow when their first batch registers rather
  than when the DAG is parsed; the register task's documentation names them.
- **Structure before declaration.** Discovery computes a clearing file's structural verdict in
  the same pass as its checksum, outside the warehouse pool, with the function the extract
  step's parser calls, so there is one definition of a whole file. A refusal records one
  primary reason, by precedence: structure, then declaration, then contract. It also reads each
  object once, where it read each twice.
- **The sanctions list's `topics` is `sensitive`**, corrected in version 1 of the unmerged
  contract with a history note rather than bumped: a topic says why a party is listed, and
  `role.pep` is a statement about a person's political role.
- **An FX row's `_source_file` is the URL requested for its date**, query included, where it was
  the endpoint with `<date>` in place of the date.
- **The backfill resumes through the REST clear with `run_on_latest_version`**, experimental in
  Airflow 3.3.2, and checks that every task of a cleared run ran on the DAG's latest version.
  Measured through its own resume path: after a DAG change every task ran on the new version;
  a CLI clear after a second change left one on the old, and the check refused it. This
  supersedes the third run's note that `make backfill` resumes with the CLI.
- **The PII scan's sanctions excuse is scoped and tested**: only under the sanctions source's
  own prefixes, and only for a value the file delivered for that snapshot carries.
  `PLANT=1` now plants a list name in a core banking object and a card reference in a snapshot,
  and requires both caught.
- **The unpaused `ops_source_tick` failure was not an out-of-order refusal.** Its log says
  `TickRefusedError: the source was seeded at profile 'ci' and this tick is running as 'dev'`:
  the Airflow containers took `NORDBANK_ENV=dev` from `.env`. And the tick could not have been
  refused for its date, because it asked for "the next day"; with the profiles matching, the
  scheduled run for 2026-09-28 would have advanced a source standing at 2026-09-18 by a day.
  Two fixes. The DAG is unscheduled and ticks to its logical date, so the state machine's
  refusal applies; and the tick's profile comes from `platform.simulation_state`, the record of
  how the source was seeded, with `NORDBANK_ENV` read only when seeding and no longer given to
  the Airflow containers. Measured before the second fix: nothing else in the containers read
  the variable — no plugin, feed path, rate, fixture or container-run script — and the delivery
  generators already read the recorded profile. No ingestion in the third run ran as `dev`.

### 2026-09-29 — the fourth acceptance run, and what it measured

The fourth run is the evidence of record: `make nuke`, a fresh stack from the code above, seed 42
at the acceptance anchor, every contract committed, the backfill over 2026-07-20 to 2026-09-18.
It was stopped once at its author's request, during 2026-08-03 after that day's reference data
had registered and before its core banking run; the stack restarted, and the backfill resumed
through the REST clear, every task of the cleared run on the latest DAG version, and ended with
exit 0. The only commit between its start and its resume changed the PII scan's report, which the
backfill does not run. 3,000 batches: 2,996 registered and 4 explicitly failed, none open;
`make ingest-integrity` clean. The identity demonstrations were repeated as in the second and
third runs and changed no registry or file-identity count.

Against the third run, 3 of 38 figures differ, and none is caused by a code change: 29 more
registered `corebank` batches and 441 more rows read and landed, all from the stop (below).
The rulings' changes are not among those figures, and each was measured on its own:

| Change | Third run | Fourth run |
|---|---|---|
| Asset events, `settlements` and `settlement_totals` | 62 and 62, one per run whose register step succeeded, registered or not | 76 and 76, one per registered batch |
| Asset events, `entities` | 11, two from no-op runs | 9, one per registered snapshot |
| Asset events against registered batches, every run | not checked | 2,996 against 2,996; 4 runs registered nothing and emitted nothing; 12 empty batches, 12 events carrying `empty_reason` |
| The cut-off file's reattempt on 2026-09-03 | declaration conflict | structurally malformed |
| `opensanctions.entities` version 1 fingerprint | `4815418e…` | `c12a7ac5…`, the only fingerprint that differs |
| FX `_source_file` | the endpoint with `<date>` | 45 distinct request URLs over 1,305 rows, each naming its row's date |
| `ops_source_tick` asked for 2026-09-25 and 2026-09-10 | not possible | refused as skipping six days and as replaying a passed day; source unmoved |
| `NORDBANK_ENV` in the Airflow containers | `dev` | unset |

Everything else the third run reported holds unchanged: 12,120 clearing records read, 12,002
landed and 118 quarantined individually; 24 injected breaks all detected at their severity and
amount; 415 of 415 card tokens resolving through both sources; 45 FX dates landed and 16 absent;
every payload faithful; 6,965 of 6,965 landed names marker names. The PII scan read 3,023
objects both ways and found no cleartext identifier, and `PLANT=1` caught all three plants. The
fault demonstration's six scenarios are identical to the third run's but for timings, the parking
demonstration's five expectations hold, and `make test-offline` passed 558 with no network.

**Finding: a resumed day re-runs reference data that had already registered.** The backfill
decides a day is complete when all forty-five `corebank` entities are registered for it, and an
incomplete day re-runs both of its relational DAGs. Stopped after 2026-08-03's reference data
registered and before its core banking ran, the resume cleared and re-ran the reference run; its
open step allocated a second batch for each of the 29 reference entities, and each re-read the
same watermark window as the first, so the 441 rows are the first batch's again. Bronze keeps
both, since it is append-only by key (ADR 0008), and nothing downstream reads bronze yet, so no
figure but the three is affected. The third run was stopped after its day's core banking had
registered and shows none of it. The first run's unexplained 29 extra `corebank` batches against
the second, recorded at the review, have the same count and probably the same cause; that
warehouse is gone, so it cannot be checked. Not fixed here: the resume should re-run only the
DAG whose entities are unregistered, and that is left for a ruling.

**Criterion 16, as ruled:** the feed assets now appear in Airflow when their first batch
registers. `ingest_macro_series/series` has registered nothing, because no FRED key is set, so it
has no asset yet; the register task's documentation names it.
