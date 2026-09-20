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

## Amendments

Appended during implementation. The scope text above is left as issued; the protocol is in
`docs/specs/README.md`.

### 2026-09-20 — The tick reaches the database through a driver, not through psql

The specification does not name a transport, and M2's tooling establishes one by precedent:
every host-side tool reaches the source through `psql` inside the container, because
`docs/project_state.md` and `generator/writer.py` record that "the host has no Postgres
driver". That is a dependency choice recorded as an environment fact, and M3 is the milestone
where the difference is load-bearing, so it was measured rather than inherited.

Measured on the workload a tick actually performs — read the state a day's decisions need
(17,907 rows), then one transaction copying 10,403 rows over five tables and folding 1,824
account balances — interleaved, PostgreSQL 16.15:

| Transport | connect | state read | write transaction | total |
|---|---|---|---|---|
| Held-open `psql` session | 71 ms | 298–386 ms | 306–353 ms | 628–765 ms |
| psycopg 3 | about 60 ms | 31–40 ms | 464–523 ms | 520–561 ms |

Isolated, with the process launch counted, which is what one `make tick` pays: `psql` one-shot
803–861 ms against psycopg 464–514 ms. Writing pre-encoded bytes in one mebibyte blocks rather
than a string made no difference to psycopg, so the simple form is used.

**The driver wins on both paths** — an order of magnitude on the state read, and roughly
1.7 times on a single-tick write — and it removes three things the pipe needs: a sentinel
protocol to frame responses, a reader thread per stream, and a hang as the failure mode when
`psql` exits mid-protocol. It also gives native transaction control, which acceptance
criterion 2 is written in terms of.

`psycopg[binary]` becomes a project dependency. It is already present in the Airflow image at
3.3.5, beside the psycopg2 2.9.13 the Postgres provider brings, so `ops_source_tick` needs no
image change.

**The existing psql tooling stays.** `make schema-apply` runs SQL files mounted inside the
container, which a host driver cannot reach, and every M2 target is built on it.
`schema_contract.Executor` was written to abstract exactly this difference. The negative
consequence is two transports in one repository, and the rule that keeps them apart is stated
here: the driver is for the tick, psql is for anything that applies a file from inside the
container.

**The rejected alternative, and the risk that made it plausible.** The pipe cannot be
shadowed: `docker compose exec` addresses the container by name. The driver depends on the
published port actually reaching it, and on the machine this was measured on it did not.
`docker compose ps` reported `0.0.0.0:5432->5432/tcp` while a native PostgreSQL 18 service
owned the port; the stack came up healthy and nothing said otherwise, and the driver reached
the wrong server and failed authentication. Two mitigations, both required:

- The connection asserts on open that it reached the Nordbank source — server major version
  and the three schemas — rather than assuming it.
- An authentication failure or a failed assertion names the likely cause: another PostgreSQL
  bound to that port, `docker compose ps postgres-source` to check, `POSTGRES_SOURCE_PORT` in
  `.env` to move it.

The committed default in `.env.example` stays at 5432. Moving it would force an `.env` edit on
every existing machine to fix a collision that only some machines have, and an error message
that explains itself covers the rest.

Recorded in ADR 0012.

### 2026-09-20 — `require_stack()` leaves the tick path

Every host-side target calls `db.require_stack()` first, which runs `docker compose ps` at a
measured 530 ms. Against acceptance criterion 13's two-second budget for a `ci` tick that is a
quarter of the allowance spent proving something the next call would discover anyway.

The tick does not call it. The connection failure path carries the diagnosis instead, naming
the stack as the likely cause, which is the same information a second later and 530 ms
cheaper. Every other target keeps it.

### 2026-09-20 — Criterion 13's two clauses are one budget, and the per-tick figure is the binding one

Criterion 13 asks for a `dev` tick under 10 seconds and sixty `dev` ticks under 10 minutes.
Those are the same number: sixty ticks at the per-tick ceiling is exactly 600 seconds, so the
second clause is satisfied by the first with no slack and is not independent evidence of
anything.

Both are reported, and the per-tick figure is treated as the constraint. The measured
components say where the budget actually goes, and it is not the data: a `dev` day is about
10,440 rows, which generate in roughly 132 milliseconds at the rate M2 measured and write in
under half a second. The fixed cost of reaching the database is the larger half at `ci` scale,
which is why the transport was measured before anything was built on it.

### 2026-09-20 — The disposition lag is one to fourteen days, as already delivered

Section 1 states that an alert raised on day D is dispositioned between D+2 and D+10. The
source already has a disposition lag, and it is not that one. `generator/profiles.yml` sets
`fraud.disposition_lag_days_min` to 1 and `disposition_lag_days_max` to 14,
`docs/generator_realism.md` states "one to fourteen days", and the loaded `dev` book measures a
minimum of 1, a maximum of 14 and a mean of 7.8 days over 1,579 dispositioned alerts.

Adopting D+2 to D+10 for ticked alerts would put two different lag distributions inside one
book, split at the anchor, with nothing in the data to explain the discontinuity. It would
also change the historical load and so the committed `ci` manifest, for a parameter whose
current value is already justified in the realism document.

The mutation engine reads `fraud.disposition_lag_days_min` and `disposition_lag_days_max`.
Criterion 7 measures against those, which is what "the stated distribution" means: the
distribution the realism document states, not a second one stated here.

### 2026-09-20 — Three named columns do not exist, and one becomes the drift event

Section 1 asks for in-place updates to `customers.occupation` and an income band, and to a
merchant risk score. None of the three is a column of the source. `core.customers` carries
identity, contact details, KYC status, risk band, signup date and residence country;
`core.merchants` carries a reference, a name, an MCC and a country. Section 2's "an income band
inconsistent with occupation" depends on the same two absent columns.

**Occupation and income band are dropped.** Adding them is a schema change to a merged
milestone, and it would drag the data dictionary, the classifications table, the ERD and the
committed manifest with it, to widen a change class that already has six tables in it.
`customers` keeps risk rating, KYC status, contact details and the occasional surname change,
which is ample breadth for SCD2 at M5.

**The merchant risk score becomes the additive drift event.** `core.merchants` gains
`merchant_risk_score numeric(18,8)` when the drift timeline fires it, not before. This gives
the additive event a purpose beyond demonstrating the mechanism: the column appears mid-history,
which is exactly the condition M4's additive-drift handling is written for, and the in-place
revisions section 1 asks for begin once it exists. A merchant risk score is an attribute of an
operational row rather than a vocabulary, so it belongs on `core.merchants` and not in a `ref`
lookup.

**The dependent dirt case is replaced.** "An income band inconsistent with occupation" becomes
**a KYC status inconsistent with the account activity behind it**: a customer whose
`kyc_status_code` is `pending` or `expired` while their accounts are open and transacting
normally. Both values are legal, the foreign keys are satisfied, no constraint notices, and the
combination is wrong in a way only a business rule can see — which is the property the original
case was chosen for. It is built from columns that exist, and it is realistic: KYC review
cycles lapse without the bank freezing the account the same day.

### 2026-09-20 — Soft deletes are restricted to rows that never moved money

Section 1 names voided transactions as a soft-delete target. Invariant 4 reconciles
`accounts.current_balance_amount` against posted transactions and payments and filters on
`is_posted` but **not** on `is_deleted`, which makes a soft-deleted posted transaction a fork
with no good branch: reverse the balance and invariant 4 fails, leave it and the source says
the money moved while silver, which drops `is_deleted` rows, says it did not.

A tick soft-deletes only transactions in a non-posted status — `pending`, `authorised`,
`declined`, `reversed`. **A posted transaction is reversed, not deleted**, through
`core.transactions.reversal_of_transaction_id`, which exists for this and which the historical
load already uses. That is also what a real ledger does: an authorisation is voided, a posting
is reversed, and the reversal is itself a movement with its own ledger entries.

Criterion 8's three entity types are unaffected: merged duplicate customers, non-posted
transactions and cancelled applications.

### 2026-09-20 — The balance moves at the status transition, not only at insert

Invariant 4's computed value changes whenever a transaction or payment crosses the `is_posted`
boundary, with no row inserted. A tick that flips a `pending` transaction to `posted`, or a
payment to `settled`, must apply the balance delta at that moment.

The tick therefore folds two populations into the balance, not one: the movements it inserts
with a posted status, and the rows already in the book whose status it changes into or out of a
posted one. Both are applied in the same `UPDATE ... FROM (VALUES ...)` that maintains
`current_balance_amount`, so the fold stays one statement.

### 2026-09-20 — Sequences are synchronised after the commit, never inside it

`setval` is not transactional. Measured on PostgreSQL 16.15: a `setval` inside a transaction
that then rolled back left the sequence at the value it had set. A tick that resynchronised
sequences inside its own transaction would therefore violate criterion 2's "leaves the
simulation date and all data unchanged" on the one path the criterion exists to test.

The tick assigns every identity explicitly from `max(id)`, read as part of its state read,
exactly as the loader does under `COPY`. No sequence is consumed during the transaction.
`setval` runs after the commit, for hygiene only.

A crash between the commit and the `setval` leaves a sequence behind the data. That is
harmless and is stated rather than guarded: both the loader and the tick derive identities from
`max(id)` rather than from the sequence, so nothing reads the stale value, and the next
successful tick resynchronises it.

### 2026-09-20 — Section 2 is restated, and the source performs exactly one physical delete

Section 2's conclusion stands: the dirt this milestone produces is the dirt a constrained OLTP
system genuinely emits, and genuine schema and type violations belong to the file and API feeds
at M4. Three corrections to how it argues that, and one addition.

**The argument is stronger than "injecting them would trade credibility for a demonstration".**
A foreign key that is enabled cannot be violated. An orphan in `core` is not something the
generator declines to produce, it is something the source **cannot express** — the same shape
of argument as ADR 0008, where bronze immutability is a property of key construction rather
than of restraint. Refusing to do something and being unable to say it are different claims,
and the second is the one that holds here.

**A value can be malformed by the domain's rules while conforming to the column's.** The
constraints bound the column, not the semantics. `core.payments.counterparty_iban` is
`varchar(34)` checked against `^[A-Z]{2}[0-9A-Z]{13,32}$`, and nothing in the platform
validates the mod-97 checksum — `docs/generator_realism.md` already records that the generated
IBANs carry none. That is a real validation failure for a contract to catch at M4, produced by
a source that violates no constraint, and it is the example to name because a `varchar` cannot
express the rule that would catch it.

**The drift widening is itself a type divergence, produced with nothing relaxed.** Rows
extracted before the event and after it carry different types for the same column. Section 2's
claim that a constrained source cannot emit a type violation is true of a single row at a
single moment and false of the same column over time, and section 3's own mechanism is the
counterexample.

**Physical deletes: exactly one, and it exists so that a control at M7 is testable.**
`docs/architecture.md` records that watermark extraction cannot detect a `DELETE` and schedules
a primary-key reconciliation at M7 to find the resulting orphans in silver. If nothing in the
source ever performs a physical delete, that reconciler can never be demonstrated against a
known positive, which is worse than not having it: an untested control reads as a working one.

The source therefore performs one physical delete, narrowly:

- **Only a duplicate customer record that no other row references.** The tick's own duplicate
  generator creates these, so the population is known; a customer with an account, an
  application, a loan, a login session or an alert is never a candidate. A dependent-free row
  cannot violate a foreign key, so the delete needs nothing relaxed and nothing deferred.
- **At a small stated rate**, parameterised like every other change class.
- **Logged as its own operation class in `platform.tick_log`**, so M7 validates its reconciler
  against the exact set of keys that vanished rather than against a guess.

This is the only physical delete the source performs, and the reason is stated in section 2
rather than left to be discovered: some systems purge merged duplicates outright instead of
tombstoning them, and the platform needs one real instance of the condition its M7 control is
written to detect.

### 2026-09-20 — `schema-check` computes the expected schema as the dictionary plus fired drift

Section 3 says the drift event changes the source DDL and the data dictionary together so that
`schema-check` stays green. Measured, the three ways it disagrees are exact: adding a column
and widening a type produced `undocumented column`, `type disagrees`, and, once the drift also
classified the new column, `classified column is not documented`.

"Together" cannot mean the tick edits `docs/data_dictionary.md`. That file is committed and is
the parser's input; a tick that rewrote it would make `git status` a function of how many ticks
had been run and would leave a modified tracked document behind every CI run. Nor can the
dictionary ship the post-drift state, because then `schema-check` is red from `schema-apply`
until the event fires.

**`schema-check` computes the expected schema as the committed dictionary plus the drift events
already fired**, reading which have fired from `platform.drift_log`. The timeline in
`generator/drift/` declares each event's DDL and its dictionary delta as one object, so one
source per fact still holds: the dictionary is the base contract, the timeline is the delta,
and neither is maintained by hand against the other. Nothing committed is edited at runtime,
and a checkout with no database parses the dictionary exactly as it does today.

Two consequences follow.

**The drift event writes its classification row in the same transaction as its DDL.** PostgreSQL
DDL is transactional and this was verified: inside the tick's transaction the added column was
present, and after a rollback it was gone. So a half-applied drift event is not a state the
source can reach.

**`make schema-apply` re-applies the fired events rather than refusing.** It reloads
`platform.column_classifications` from the dictionary, which would otherwise drop the
classification a drift event added and turn `schema-check` red. Refusing while `drift_log` is
non-empty would make `schema-apply` unusable after any tick. Re-applying is deterministic from
the log, and the re-applied DDL is written to be idempotent, like every other file in
`infra/docker/postgres-source/schema/`.

### 2026-09-20 — `seed` resets the whole simulation, including fired drift

Section 7 says `seed` resets `platform.simulation_state` to the anchor date. It has to reset
more than that. `make seed` currently truncates the sixteen `core` tables and leaves `ref` and
`platform` alone.

`seed` now also truncates `platform.tick_log` and `platform.drift_log`, resets
`platform.simulation_state` to the anchor, and **reverts every drift event the log says has
fired**. A schema that is post-drift while the log says nothing has fired is an incoherent
state: `schema-check` would compute an expected schema without the drift and fail against a
database that has it, and the failure would surface far from its cause.

Each drift event therefore declares its reversal alongside its application, and both are
idempotent.

### 2026-09-20 — Fuzzy matching is computed in the report, and no extension is installed

Criterion 11 asks for duplicate customer records that are fuzzy-matchable, reported with the
similarity that matched them. `pg_trgm` and `fuzzystrmatch` are available in the image and the
application role can install them — measured: `similarity('Jan Kowalski', 'Jan Kowalsky')` is
0.733 and `levenshtein` is 1.

Neither is installed. The criterion asks for a report, not a database capability, and an
extension in the source database is a schema change that `schema-check` and the design rules
would have to account for — `similarity` returns `real`, and design rule 1 prohibits floating
point in these schemas, so a stored score would need care that a reported one does not.

The report computes similarity in Python and states its function and threshold.

### 2026-09-20 — A per-tick delta guard on invariants 4 and 13, with the full suite still the gate

Section 5 requires all fourteen invariants to hold after an arbitrary number of ticks, and
criterion 5 verifies them after sixty. Nothing requires checking between ticks, and checking
between ticks with the existing suite is not affordable: `make seed-verify` on the `dev` book
measures 138 to 141 seconds, so thirty ticks would spend seventy minutes verifying.

Measured per invariant on `dev`, in one session totalling 139.4 seconds:

| Invariant | 4 | 13 | 12c | 6 | 5 | 14b | 11 | 14 | 1 | 9a | 2 | the rest |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ms | 73,593 | 56,029 | 2,506 | 1,534 | 1,049 | 1,030 | 980 | 843 | 464 | 376 | 324 | under 30 |

Invariants 4 and 13 are 93 per cent of the run, and they are exactly the two that scope
cheaply to a tick's delta: invariant 4 to the accounts the tick touched, invariant 13 to the
transactions it booked. Both are correlated per-row checks whose cost is the size of the
population, so restricting the population is the whole saving.

**A tick therefore runs both, scoped to its own delta, before it commits.** A broken tick fails
at once and rolls back, rather than being discovered thirty ticks later with no way to tell
which tick did it. The guard is inside the tick's transaction, so a failure leaves nothing
behind.

**The full suite is unchanged and remains the gate.** `make seed-verify` runs all fourteen over
the whole book exactly as it does today, and criterion 5 is measured with it. The guard is an
early warning, not a substitute: it cannot see a violation a tick creates outside the rows it
touched, which is precisely what the full run is for.

None of the fourteen becomes more expensive to check under mutation. They were full-book scans
before the first tick and they are full-book scans after the sixtieth, over a book that thirty
`dev` ticks grow by about three per cent.

### 2026-09-20 — CI runs a short tick sequence; the sixty-tick run is a reported local acceptance test

Criterion 5 asks for sixty ticks on `ci` and criterion 4 for sixty more to prove replay
determinism. Putting both in the `stack` workflow would add the tick sequence twice to a
required check that already builds an image, starts nine services, applies a schema and seeds
a profile.

`stack.yml` runs a short sequence — enough to prove the wiring, the tick log reconciliation and
one drift event firing with `schema-check` still green. The sixty-tick sequences, the replay
determinism comparison and the thirty-tick `dev` run are local acceptance tests, run and
reported against this specification.

**The simulated clock may run past the real date, and M4 has to decide what that means.** The
`ci` profile pins its anchor, so sixty ticks land beyond today. Nothing in the source objects:
the only real-clock constraint in the schema is
`core.customers.date_of_birth < current_date`, which a simulated future date does not touch.
The runbook records that the simulated clock is free to run ahead of the real one.

M4 inherits a constraint from this that it should not discover by hitting it, so it is recorded
in `docs/project_state.md` now: **the ECB feed cannot return a rate for a simulated date beyond
the real one.** Either the anchor is chosen so the extracted window stays behind real time, or
the feed synthesises rates for simulated-future dates and says so. Both are defensible and the
choice should be deliberate, which is why it is written down before the milestone that makes
it.
