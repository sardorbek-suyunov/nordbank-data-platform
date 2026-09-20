# 004 — Mutation Engine

Status: Approved
Depends on: 000, 001, 002, 003

## Goal
Advance the simulated source system by one business day, producing the kinds of
change that incremental extraction has to cope with: inserts, in-place updates,
soft deletes, late-arriving events, plausible dirt, and scripted schema drift.
This is M3, and it exists so that M4's extraction layer has genuine material
rather than a static snapshot.

## Simulation clock: the design decision this milestone turns on

A tick simulates a date in the past. An `UPDATE` during that tick fires
`core.set_updated_at()`, which sets `now()` — the real wall clock — and every
historical row would carry today's watermark. That defeats the entire milestone.

**Resolution: the trigger reads a simulation clock, with `now()` as fallback.**

```sql
new.updated_at := coalesce(
nullif(current_setting('nordbank.sim_now', true), '')::timestamptz,
now()
);
```

The tick sets `SET LOCAL nordbank.sim_now` for its transaction. Design rule 4
holds unchanged in substance — the trigger still owns `updated_at`, no code path
assigns it directly — and the simulated clock is explicit, transactional and
visible in the catalogue. Record it as an amendment to spec 002 design rule 4,
and state the negative consequence: a session that sets the variable and then
performs unrelated writes would backdate them, so setting it is confined to the
tick's own transaction with `SET LOCAL`.

## Tick semantics

- State lives in `platform.simulation_state`: current simulated date, seed,
  profile, tick sequence number, last tick completed at.
- `make tick DATE=D` requires the current state to be exactly D−1. Out-of-order
  or skipped dates are refused with the expected date named. No implicit
  catch-up.
- `make tick-to DATE=X` advances one day at a time to X, which is what M4's
  backfill needs.
- A tick is a state transition, not idempotent. What is guaranteed is
  **replayability**: the same seed, the same starting state and the same date
  produce the same result. The per-tick random substream derives from
  `blake2b(seed ‖ "tick" ‖ date)`, so tick(D) does not depend on how many ticks
  preceded it.
- A tick is one transaction. A failed tick leaves the state untouched and the
  date unadvanced.

## Scope

### 1. Change classes
Each is parameterised in `generator/profiles.yml`, justified in
`docs/generator_realism.md`, and reported per tick.

**Inserts.** New customers at the acquisition rate, their accounts, cards and
addresses; applications and disbursed loans; the day's transactions, payments,
login sessions and GL batches; new fraud alerts.

**In-place updates.** These are what SCD2 and deduplication at M5 exist to
handle, so breadth matters more than volume:
- `customers`: risk rating, KYC status, occupation, income band, contact
  details, and occasionally a surname change.
- `customer_addresses`: a new version inserted and the previous one's validity
  closed, which is an insert and an update in one logical change.
- `accounts`: status transitions through active, dormant and closed; overdraft
  limit changes.
- `cards`: blocked, replaced, expired and reissued.
- `loans` and `loan_installments`: status transitions, and payments recorded by
  updating `paid_amount` and `paid_at`.
- `merchants`: risk score revisions.
- `fraud_alerts`: disposition. **This one is deliberately lagged** — an alert
  raised on day D is dispositioned between D+2 and D+10 on a stated
  distribution, which is precisely why `metric_definitions.md` attributes alert
  precision to the disposition month rather than the alert month. The lag is
  the evidence for that decision.

**Soft deletes.** `is_deleted` set on rows a real system would logically
remove, across at least three entity types: merged duplicate customer records,
voided transactions, cancelled applications. Rates small and stated. The row
continues to arrive through the normal incremental path; nothing is physically
removed.

**Late-arriving events.** Rows inserted during tick D whose business timestamp
is earlier than D: offline card transactions clearing two to five days late,
payments settling after initiation, installment payments posted late. Their
`updated_at` is the tick's simulated time and their business timestamp is in
the past. This is the entire reason bronze partitions by ingest date and silver
orders by business time, so it must be measurable, not incidental.

### 2. Dirt: what a constrained source can and cannot produce
`core` has foreign keys, check constraints and not-null constraints, so it
**cannot** produce type violations, orphans or malformed values. Injecting them
would require relaxing spec 002's integrity guarantees, which would be trading
the platform's credibility for a demonstration.

So the dirt this milestone produces is the dirt a well-constrained OLTP system
genuinely emits:
- Inconsistent casing and stray whitespace in free-text fields.
- Duplicate customer records: the same person entered twice, both rows
  individually valid, detectable only by fuzzy matching. This is the most
  valuable item here, because it gives silver a real entity-resolution problem.
- Legal but implausible values: a transaction at 03:00, an income band
  inconsistent with occupation.
- Nullable columns left null where business logic expects a value.
- Unicode confusables and diacritic inconsistency in names.

State explicitly in the spec and in `architecture.md`: genuine schema and type
violations belong to the file and API feeds at M4, where they can actually
occur, and that is where quarantine will earn its keep. A source that emits
invalid rows through a constrained interface would be theatre.

### 3. Scripted schema drift
`generator/drift/` holds a timeline of drift events, each declaring a simulated
date, a type, the affected table and column, and the behaviour the platform is
expected to exhibit when it meets it.

M3 implements the mechanism plus two events: one additive column and one type
widening. The rest are deferred to M4.

The drift event changes the source DDL **and** the data dictionary together, so
`schema-check` stays green. That is the correct model: drift is divergence
between the platform's *contract* and reality, not between the source and its
own documentation, and contracts do not exist until M4. Record that reasoning
so M4 has the hook: the contract in `contracts/` is versioned separately and
lags deliberately.

### 4. Tick log
Every tick writes to `platform.tick_log`: simulated date, tick sequence, seed,
duration, and per-table counts of rows inserted, updated, soft-deleted and
late-arriving.

This is not telemetry, it is a reconciliation control. At M4 the platform can
assert that bronze received exactly what the source says it changed, and at M7
that becomes a reconciliation check. Design it for that consumer.

### 5. Coherence after mutation
All fourteen invariants from spec 003 must pass after an arbitrary number of
ticks. This is the hard part, because the tick has to maintain coherence
incrementally rather than recompute it:
- `accounts.current_balance_amount` is updated as movements post, so invariant
  4 still holds exactly.
- GL batches balance per currency, and daily debits equal credits, on every
  tick.
- Installment schedules stay consistent with their loan's terms.
- No event lands outside the lifetime of the account, card or loan it belongs
  to, including when a tick closes an account.

### 6. Airflow integration point
`airflow/dags/ops_source_tick.py`, DAG id `ops_source_tick`, `@daily`,
**paused by default**, one task advancing the source by one day. M4 uses it to
interleave ticks with extraction; M3 only proves the wiring. It acquires no
warehouse pool, since it touches only the source database.

### 7. Make targets
- `tick` — advance one day, reporting the change counts.
- `tick-to` — advance to a date.
- `tick-status` — print the simulation state and the last ten tick log rows.
`seed` resets `platform.simulation_state` to the anchor date.

### 8. Tests
- Unit: per-tick substream derivation, state machine refusals, late-arrival
  date arithmetic, drift timeline selection.
- Integration: `ci` seed, sixty ticks, all fourteen invariants pass; tick log
  counts reconcile against rows whose `updated_at` falls in the tick window;
  replay determinism.

## Out of scope
Extraction, contracts, bronze, quarantine, dbt, the sanctions or FX feeds,
settlement files.

## Acceptance criteria
1. `make tick` advances one day and refuses an out-of-order or skipped date,
   naming the date it expected.
2. A tick is one transaction: an induced failure mid-tick leaves the simulation
   date and all data unchanged. Demonstrate it.
3. No row written or updated by a tick carries a wall-clock `updated_at`.
   Show the distribution across a sixty-tick run.
4. Replay determinism: seed `ci`, tick sixty days, hash every table; nuke,
   repeat, identical hashes.
5. All fourteen spec 003 invariants pass after sixty ticks on `ci` and after
   thirty on `dev`.
6. Every change class is present and measurable over sixty ticks: report counts
   of inserts, in-place updates by table, soft deletes by table, and
   late-arriving rows with the distribution of their business-timestamp lag.
7. Fraud alert dispositions lag their alerts on the stated distribution. Report
   measured against stated.
8. Soft deletes occur across at least three entity types.
9. `platform.tick_log` counts reconcile exactly against rows whose `updated_at`
   falls within each tick's window, for all sixty ticks.
10. At least one drift event fires within sixty ticks, the source schema
    changes, and `schema-check` remains green.
11. Duplicate customer records are present and fuzzy-matchable; report how many
    and by what similarity.
12. `ops_source_tick` exists, is paused, imports cleanly, and succeeds when
    triggered manually.
13. Tick runtime: `ci` under 2 s, `dev` under 10 s, both measured. Sixty `dev`
    ticks under 10 minutes, so an M4 backfill is practical.
14. All existing checks pass; delivered as a pull request on
    `feat/M3-mutation-engine` with all four required checks green.
