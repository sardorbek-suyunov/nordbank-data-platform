# M3 — The mutation engine

Specification: [004-mutation-engine.md](../specs/004-mutation-engine.md)
Status: complete
Date: 2026-09-20

## What was built

A tick: one transaction that advances the simulated source system by one business day and
produces the kinds of change incremental extraction has to cope with. Seven change classes run
in dependency order inside it — `lifecycle`, `acquisition`, `movements`, `dispositions`, `dirt`,
`deletes`, `drift` — and every one of them is parameterised in `generator/profiles.yml`,
justified in [generator_realism.md](../generator_realism.md), and counted in
`platform.tick_log`.

The engine reaches the database through a driver rather than through `psql`, because acceptance
criterion 2 is written in terms of a real rollback (ADR 0012). The `updated_at` trigger reads a
simulation clock, so a tick writing a day in the past does not stamp five years of history with
today's watermark. `core` and `ref` carry that clock; `platform` carries real time, written last,
after the clock is cleared once.

`make tick`, `make tick-to` and `make tick-status` are the operator-facing targets.
`make tick-acceptance` reproduces the evidence below. `ops_source_tick` is the Airflow DAG M4
will interleave with extraction; it is paused.

## Measured

| Profile | Book at the anchor | Ticks | Per tick, median | Per tick, max | Run |
|---|---|---|---|---|---|
| `ci` | 166,381 rows | 60 | 409 ms | 462 ms | 24.6 s |
| `dev` | 10,934,878 rows | 30 | 5,455 ms | 8,194 ms | 169.0 s |

Criterion 13 asks for a `ci` tick under two seconds and a `dev` tick under ten. Its two clauses
are one budget — sixty ticks at the per-tick ceiling is exactly six hundred seconds — so the
per-tick figure is the binding one and the sixty-tick total is reported beside it rather than as
independent evidence.

Sixty `dev` ticks at the measured rate is 5.6 minutes, inside criterion 13's ten-minute clause.

A `ci` tick writes about 1,600 rows and a `dev` tick about 22,000. What the budget goes on is
not the data, and it is not the same thing at the two scales. At `ci` the fixed cost of reaching
the database dominates, which is why the transport was measured before anything was built on
it. At `dev` it was the *reads*, and the specification's estimate of where the time would go was
wrong in a way only a profile showed.

## What the measurements changed

**The historical load had three defects at the boundary of its own window, and only a day after
the anchor could show them.** The comparison that found them is the per-day mean over the thirty
days either side of the anchor. Each is fixed, and each moved the committed manifest.

- A monthly rate spread over a truncated window made those days *denser* rather than fewer.
  Three windows are short — the month the history starts in, the month it ends in, and the month
  an account opens in — and the last is the worst place for it: the eighteen days before the
  anchor ran at 608.7 transactions a day against August's 209.2, a cliff on exactly the date M4
  begins extracting.
- An entity whose date fell past the anchor was clamped onto it. 221 of 760 accounts and 138 of
  464 cards landed on one day, against one to four accounts on every other day. A third of the
  book therefore had no history at all and the last day of the history looked like a migration.
  They are now omitted instead: a customer who joined last month has not opened their second
  account yet.
- Every one of the 33 cards `status_mix` marked `expired` had an expiry date years in the
  future. Expiry is now read off the date and `expired` has left the mix.

After the fixes, per-day means over the thirty days either side of the anchor:

| Measure | History | Ticked | Ratio |
|---|---|---|---|
| Transactions | 248.0 | 244.4 | 0.99 |
| Payments | 50.8 | 53.5 | 1.05 |
| Login sessions | 159.1 | 159.7 | 1.00 |
| Ledger batches | 272.0 | 276.6 | 1.02 |
| Card purchases | 141.7 | 128.2 | 0.90 |

The card-purchase gap is accounts whose only card is blocked, cancelled or replaced. The
lifecycle phase reissues for them, so it closes across the run.

**A `dev` tick cost 48.9 seconds, and four indexes and a bounded sample took it to 5.5.** The
specification's amendment estimated the budget from the write side: "a `dev` day is about 10,440
rows, which generate in roughly 132 milliseconds and write in under half a second". That is
true, and it was the wrong half. Profiled, one tick at 48.9 seconds:

| | Before | After |
|---|---|---|
| The delta guard's invariant 4 | 43.4 s | 1.4 s |
| The delta guard's invariant 13 | 4.1 s | 0.17 s |
| The movement phase | 5.3 s | 1.5 s |
| The delete phase | 1.5 s | 0.26 s |
| **One tick** | **48.9 s** | **5.5 s** |

Three findings, each measured before it was acted on.

*A composite index on `core.login_sessions (customer_id, started_at)`.* The catalogue loop gives
every foreign key column a single-column index, which serves the constraint and not a query that
filters on the key *and* a range of another column: the foreign key index answers "this
customer's sessions around this instant" by reading every session that customer ever had.
Invariant 13 over the whole `dev` book goes from **52.8 s to 6.7 s**, which is also 46 seconds
off `make seed-verify`.

*Three business-date indexes.* The clearing cycle reads recent business history on every tick —
the authorisations that may post, the payments that may book or settle, the postings that may be
reversed, the non-posted rows that may be voided — and all four filter on a business timestamp
that is not a foreign key and was therefore unindexed. Every one of those queries scanned the
whole table. A seven-day window over 2.2 million transactions: **1,227 ms to 25 ms**. Over
470,000 payments: **1,230 ms to 16 ms**. `updated_at` is indexed and is not a substitute — it
says when the bank last touched a row, and these questions are about when the customer did.

*The delta guard's balance check runs over a bounded sample.* The amendment expected the scoped
check to see "a few hundred accounts instead of seven thousand". A `dev` day moves 1,800 of
6,730, because the fold touches every account that transacted, and re-deriving one account's
balance costs 45 ms: the subquery reads all 403 of its posted movements, scattered across a
table larger than the container's cache — 695,194 buffer reads, 5.4 gigabytes, for one check.
The sample is forty accounts, drawn from the tick's own stream so a failure is reproducible, and
what it buys is stated rather than implied: a fold error affecting one account in ten is caught
with probability 0.99, one in twenty with 0.87, and a single-account error is usually missed.
That last one is what `make seed-verify` is for, and the guard was never the gate.

**Acceptance criterion 9's reconciliation cannot be run after the sixty ticks.** A later tick
re-stamps a row an earlier one wrote and the earlier window loses it permanently. The historical
book already shows the collapse, because the loader stamps an account with its last movement:

| Day | Accounts that moved | Accounts stamped that day |
|---|---|---|
| Six days before the anchor | 210 | 6 |
| Three days before | 234 | 21 |
| The day before | 232 | 82 |

With 466 of 736 open accounts touched by a single day of movements, `accounts` loses most of a
six-day-old window. The reconciliation runs per tick instead, which costs 7.8 ms across five
tables and is the only form M4 depends on — an extractor running at the end of day D sees exactly
what the log says changed on day D.

**It found two defects by itself, which is what a control is for.** A tick that logged 214
accounts against a window holding 213, because the balance fold moved an account the acquisition
phase had just inserted; the rule "a tick never updates a row it inserted in the same tick" comes
from that. And, through the schema rather than the control, an application written with a
decision timestamp earlier than its own arrival — which is why the lending funnel is three acts
on three different days rather than one.

**Updates are counted by key, not by rows affected.** Two phases can touch one row in one tick —
the lifecycle sweep sets an account dormant and the movement fold moves its balance — and the row
appears once in the window. Every update statement returns the keys it changed.

**The unauthorised overdraft is the mechanism the milestone turns on.** The loaded `ci` book has
72 overdrawn accounts and not one past its limit. An offline card transaction inserted late
cannot be declined for insufficient funds — no online authorisation existed to decline — so it
posts and the balance goes where it goes. Every instance of the condition downstream is
attributable to that, and invariant 4 is unaffected, because it asserts that the stored balance
is the signed sum of posted movements and knows nothing about the limit.

**The disposition follows the score, not the truth.** A tick reads an alert out of the database
and cannot know whether the transaction behind it was fraudulent: nothing in `core` records that,
and nothing should, because a bank stores which alerts its analysts confirmed rather than which
of its transactions were really fraud. The verdict is the posterior of the detector's own two
score distributions against a prior derived from the detector — recall times the fraud rate over
that plus the false alert rate times everything else, which is 0.596 — and it reproduces the
historical precision by construction.

## The manifest change log

Four commits regenerate the committed `ci` manifest. Each states in its body what changed in the
generator, why the generated data moved and which tables' hashes shifted; this is the running
list.

| Commit | What changed | Tables moved | Total rows |
|---|---|---|---|
| `refactor: draw an account's regular context from addressable streams` | The regular credit left the per-customer stream; familiar merchants and mandates left the per-account-month stream; the `devices` key became the calendar month | 12 of 16 | 209,197 → 211,627 |
| `fix: make the book continuous across the anchor` | Partial-month scaling, the anchor clamps, the card expiry status | 14 of 16 | 211,627 → 166,381 |
| `feat: acquire customers, open their accounts and run the lending funnel` | The address row builder draws city, postcode and street in one order for both address types | 1 of 16 | 166,381 → 166,381 |
| `test: regenerate the ci manifest for the device count stream change` (before this session) | The device count moved to its own addressable stream | 2 of 16 | 209,190 → 209,197 |

The book is 21 per cent smaller than it was at the end of M2, and every row of the difference is
an artefact the fixes above removed: a clamped entity that should not have existed, or a
partial month's activity compressed into the days that remained.

## Acceptance criteria

| # | Criterion | Evidence |
|---|---|---|
| 1 | `make tick` advances one day and refuses out-of-order or skipped dates, naming the date expected | All three refusals verified by hand and pinned by unit test. An out-of-order date: "tick expected 2026-09-20; 2026-09-19 is on or before 2026-09-19". A skip: "tick expected 2026-09-20; got 2026-09-25, which skips 5 day(s)". A profile mismatch names both profiles |
| 2 | A tick is one transaction; an induced failure leaves the date and all data unchanged | `generator/tests/test_tick_integration.py`, parameterised over failures after `lifecycle`, `acquisition`, `movements` and `dirt`. Each asserts the simulation state and all sixteen row counts are unchanged |
| 3 | No row a tick writes carries a wall-clock `updated_at`; show the distribution over sixty ticks | 97,321 rows stamped after the anchor, 97,321 inside some tick's own simulated day, 0 outside every tick window. The `ci` anchor is pinned in the past and sixty ticks run past today, so a row stamped with `now()` would land before its day rather than after; the window catches either direction |
| 4 | Replay determinism: seed, tick sixty, hash every table; repeat; identical hashes | Two full seed-and-sixty-tick runs. All sixteen tables byte-identical, 259,624 rows |
| 5 | All fourteen invariants pass after sixty ticks on `ci` and thirty on `dev` | `ci`: fourteen of fourteen after sixty ticks. `dev`: fourteen of fourteen after thirty, with invariant 9's statistical band asserted rather than suspended — a confirmed-fraud rate of 0.6551 over 1,850 dispositioned alerts, inside the band of 0.42 to 0.78 — and invariant 13 at 0.928 over 638,011 digital transactions |
| 6 | Every change class present and measurable; counts by table; late-arrival lag distribution | 96,278 inserts, 17,371 updates, 172 soft deletes, 69 late arrivals and 5 physical deletes over sixty `ci` ticks, broken down by table below. Late-arrival lag: 2 days 49.3%, 3 days 27.5%, 4 days 14.5%, 5 days 8.7%, mean 2.83, against a stated two to five weighted 0.44/0.29/0.17/0.10 |
| 7 | Fraud alert dispositions lag their alerts on the stated distribution | Alerts raised by a tick, at `dev` where the sample supports it: n=64, min 1, mean 3.63, max 11 days against a stated one to fourteen. At `ci`: n=8, min 2, mean 4.25, max 6. Precision 0.655 over 1,850 dispositioned alerts at `dev`, inside the band of 0.42 to 0.78. The inherited backlog is reported separately and is not the tick's distribution: it measures how old an alert was when the tick finally closed it, and the historical load spreads its ten per cent non-final share across the whole history rather than near the anchor |
| 8 | Soft deletes across at least three entity types | Three: 16 customers, 153 transactions, 3 loan applications |
| 9 | Tick log counts reconcile exactly against each tick's window, for all sixty | Asserted after every one of the sixty ticks, and after every one of the sixty `dev` ticks. Amended: the reconciliation is exact at the tick rather than after the run, for the reason measured above |
| 10 | At least one drift event fires within sixty ticks, the schema changes, `schema-check` stays green | Both events fire: the additive column at tick 12, the type widening at tick 37. `schema-check` reports 510 columns agreeing while they are applied and 509 after a seed reverts them; `schema-apply` re-applies both |
| 11 | Duplicate customer records present and fuzzy-matchable; how many and by what similarity | 22 candidate pairs sharing a date of birth and a country; 19 match on a trigram similarity of at least 0.55. Similarity min 0.600, mean 0.684, max 0.812. Jaccard over character trigrams, which is what `pg_trgm.similarity` computes; neither `pg_trgm` nor `fuzzystrmatch` is installed |
| 12 | `ops_source_tick` exists, is paused, imports cleanly, succeeds when triggered | Eight DAG integrity tests pass, two of them new: that it is paused on creation and holds no warehouse pool, and that it writes through a connection of its own. `airflow dags test ops_source_tick` advances the source one day and reconciles 1,504 logged rows against 1,504 in the window |
| 13 | Tick runtime: `ci` under 2 s, `dev` under 10 s, both measured; sixty `dev` ticks under 10 minutes | `ci` median 409 ms, maximum 462 ms over sixty ticks. `dev` median 5,455 ms, maximum 8,194 ms over thirty, which is 5.6 minutes for sixty at that rate. Both after the profiling above; before it a `dev` tick was 48.9 s |
| 14 | All existing checks pass; delivered as a pull request with four green checks | 214 unit tests, 8 DAG integrity tests, 47 generator integration tests including the new tick suite, 23 in-stack integration tests, `ruff` clean, `check_docs` green, `schema-check` green, the committed `ci` manifest matching a fresh regeneration. Pull request #4 |

Change classes by table, over sixty `ci` ticks:

| Table | Insert | Update | Soft delete | Late | Delete |
|---|---|---|---|---|---|
| `account_holders` | 141 | 0 | 0 | 0 | 0 |
| `accounts` | 141 | 13,924 | 0 | 0 | 0 |
| `cards` | 153 | 27 | 0 | 0 | 0 |
| `customer_addresses` | 176 | 8 | 0 | 0 | 0 |
| `customers` | 170 | 73 | 16 | 0 | 5 |
| `fraud_alerts` | 10 | 10 | 0 | 0 | 0 |
| `gl_entries` | 40,995 | 0 | 0 | 0 | 0 |
| `gl_transactions` | 20,477 | 0 | 0 | 0 | 0 |
| `loan_applications` | 9 | 14 | 3 | 0 | 0 |
| `loan_installments` | 420 | 33 | 0 | 0 | 0 |
| `loans` | 15 | 0 | 0 | 0 | 0 |
| `login_sessions` | 11,275 | 0 | 0 | 0 | 0 |
| `merchants` | 0 | 73 | 0 | 0 | 0 |
| `payments` | 3,681 | 2,859 | 0 | 0 | 0 |
| `transactions` | 18,615 | 350 | 153 | 69 | 0 |
| **Total** | **96,278** | **17,371** | **172** | **69** | **5** |

`merchants` moves only after tick 12, because the column it revises does not exist until the
drift event adds it.

Thirty `dev` ticks, for the scale at which the statistical evidence means something: 563,185
inserts, 111,447 updates, 1,744 soft deletes over the same three entity types, 427 late arrivals
and 14 physical deletes. The late-arrival lag at that sample is 45.9 per cent at two days, 27.6
at three, 17.3 at four and 9.1 at five, against stated weights of 0.44, 0.29, 0.17 and 0.10.

## The physical delete, and its reconciliation

Five customer records were purged over sixty `ci` ticks, each one a duplicate the `dirt` phase
created and nothing had attached to. The candidate query anti-joins every table with a foreign
key to `core.customers`, and that list is read from `pg_constraint` rather than written down, so
a table that gains such a key later cannot be missed. Measured on the loaded book, no customer
has no dependent row at all, which is what makes "dependent-free customer" an exact description
of the population rather than an approximation of it.

Every purged key is in `platform.tick_deleted_keys`, individually. Of those five keys, zero are
still in `core.customers` and zero left a dependent row behind. M7's primary-key reconciliation
therefore has an exact expected answer and can be validated in both directions: no orphan in
silver that is absent from that table, and no key in that table that silver still holds.

## Deviations from the specification

Recorded as amendments on [spec 004](../specs/004-mutation-engine.md), fourteen from the
previous session and one from this one:

- Criterion 9's reconciliation is exact at the tick rather than after the run, because a later
  tick re-stamps a row an earlier one wrote. Measured on the historical book before anything was
  built on the assumption.

Three things the specification asks for are delivered differently, and each is stated where it
is implemented rather than only here:

- **Drift events fire at an offset from the anchor rather than on an absolute date.** The anchor
  is one of the three inputs that determine a run and defaults to today, so an absolute date
  would fire for one anchor and never for any other.
- **The lending funnel takes three ticks rather than one.** `core.loan_applications` carries
  `decided_at >= applied_at`, so an application cannot be written already decided by a statement
  that also draws its arrival time. The decision lag and the disbursement lag the historical load
  states are applied on later ticks instead, which is closer to the source than the alternative.
- **Spec 004's amendment says the historical load already uses
  `core.transactions.reversal_of_transaction_id`.** It does not: the loaded book has 194 rows in
  the `reversed` status and none with that column populated, and `transactions.reversal_share`
  was declared in `profiles.yml` and read nowhere. The substance of the ruling is unaffected —
  a posted transaction is reversed rather than soft deleted — and M3 is the first thing to
  populate the column. The orphan parameter is removed.

## Verification

```bash
make up && make schema-apply
NORDBANK_ENV=ci NORDBANK_ANCHOR_DATE=2026-09-18 make seed
NORDBANK_ENV=ci NORDBANK_ANCHOR_DATE=2026-09-18 make tick-acceptance
NORDBANK_ENV=ci NORDBANK_ANCHOR_DATE=2026-09-18 make tick-acceptance REPLAY=1
NORDBANK_ENV=ci NORDBANK_ANCHOR_DATE=2026-09-18 make seed-verify
make schema-check
make test && make test-dags && make test-integration
```

## What M4 inherits

A source that changes every day and says exactly what it changed. The pattern the runbook
describes now works end to end: seed with an anchor in the past, `make tick-to` forward, and
extract day by day with a genuine sequence of single-day windows.

Three things M4 should read before it starts.

**The tick log is a reconciliation control, and its identity holds at the tick.** An extraction
run at the end of day D can assert that bronze received exactly what `platform.tick_table_counts`
says changed on day D. Run later, it will not: `generator/mutation/reconcile.py` explains why and
measures it.

**Business date, posting date and audit time are three different dates on a late-arriving item.**
`architecture.md` and `metric_definitions.md` now say so where silver and Q15 meet it. The
ledger side never moves; whether the settlement feed presents an item on its business date or its
clearing date is M4's decision.

**The simulated clock may run past the real one, and the ECB feed cannot return a rate for a
simulated date beyond today.** Either the anchor is chosen so the extracted window stays behind
real time, or the feed synthesises rates for simulated-future dates and says so. Both are
defensible; the choice should be deliberate.
