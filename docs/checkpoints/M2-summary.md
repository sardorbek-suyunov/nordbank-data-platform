# M2 — Source system schema and the deterministic historical load

Specifications: [002-source-system-schema.md](../specs/002-source-system-schema.md) version 2,
[003-historical-load.md](../specs/003-historical-load.md)
Status: complete
Date: 2026-09-19

## What was built

The source system: a schema for a simulated bank, and a generator that fills it with a coherent
operating history that the same three inputs reproduce byte for byte.

**The schema half**, delivered earlier in the milestone: 16 `core` tables, 29 `ref` tables and
`platform.column_classifications`, every foreign key indexed, an `updated_at` index and trigger
on every table, and a deferred constraint trigger that makes an unbalanced ledger batch
impossible to commit. The column-level data dictionary generates the classification table, and
`make schema-check` fails if the live schema and the document disagree.

**The data half**: `generator/` as an importable package — a seeded sub-stream random source,
profile parameters in a committed YAML file, the realism models, the entity generators, a `COPY`
loader, fourteen coherence invariants and a run manifest. `make seed`, `make seed-verify` and
`make seed-manifest`, with the last two wired into CI.

## Measured

| Profile | Customers | History | Transactions | Total `core` rows | Generate | Load | Total |
|---|---|---|---|---|---|---|---|
| `ci` | 500 | 6 months | 41,393 | 209,190 | 3.2 s | 7.4 s | 10.6 s |
| `dev` | 5,000 | 3 years | 2,279,943 | 11,274,907 | 160.5 s | 153.0 s | 313.5 s |

`ci` is inside its twenty second budget with room. `dev` is 13 seconds over its five minute
target, which is reported rather than tuned away.

`full` was not run to completion and nothing here claims it was. Projected from the `dev`
measurement it is about eighty times the work: roughly 900 million rows and several hours. That
projection is also what shows the specification's three profile targets to be mutually
inconsistent, which is recorded as an open decision in [project_state.md](../project_state.md).

## What the measurements changed

Five decisions in this milestone were settled by a measurement that contradicted the
expectation. Each is recorded in the specification amendment or the decision record that owns
it.

**`COPY` accepts explicit values into a `generated always as identity` column.** The
specification expected it to refuse and leaned towards relaxing the primary keys. It does not
refuse, while `INSERT` still does, so the schema keeps the guarantee design rule 6 was written
for and no primary key changed. `setval` after the load became mandatory instead, because `COPY`
does not advance the sequence and the failure is invisible until M3's first insert. ADR 0011.

**The deferred balance trigger's cost is a query plan, not a memory bound.** ADR 0009 identified
the queue as the constraint. On a freshly truncated `core.gl_entries` the planner has no
statistics, the trigger's cached plan scans the whole table once per row, and the commit is
quadratic: 18 s at 20,000 entry rows, 74 s at 40,000, 300 s at 80,000. With an `ANALYZE` inside
the loading transaction the same commits are linear and take under a second. The 100,000-row
chunk figure survives, for a different reason than it was chosen for.

**Foreign key validation costs 63 percent of the load; secondary indexes cost 8 percent.** So
the loader drops the first and keeps the second. Both were measured rather than assumed, and the
8 percent is the more useful number: it is the optimisation that looked obvious and did not pay.

**Chi-square is the wrong test for Benford conformity here.** It measures significance rather
than effect size and its power grows with the sample, so a fixed critical value is stricter at a
larger profile — the opposite of the scale-invariance it was chosen for. The same distribution
measures 68.6 at `ci` and 4,185.7 at `dev` while its mean absolute deviation barely moves,
0.00546 against 0.00551, both inside Nigrini's 0.006 threshold for close conformity. Invariant 14
asserts the deviation and reports the chi-square.

**Process launches and buffer sizes dominated both halves.** Reaching the database means
launching a process inside a container, at 0.589 s each: one session per reference table, per
core table and per ledger chunk cost 16.5 s, 18 s and 29 s respectively. Writing 1.6 GB of spool
a row at a time through an 8 KiB buffer had `dev` generating at 8,000 rows a second against
62,000 for `ci`. Fixing both took `dev` from about 23 minutes to 5.

## What the invariants caught

The fourteen checks were not a formality. Each of these was a real incoherence the generator
produced and an invariant refused:

- Transactions dated after the account closed, because attrition was decided after a month's
  movements rather than before. 223 rows at `ci`.
- A card issued onto an account that closed days later, because cards are issued up to ten days
  after opening. Fixed with a thirty day floor on account life, which is also what real accounts
  have.
- Loan repayments landing on closed accounts, because the lending pass runs before closures are
  decided. 1,155 rows at `dev`. Fixed by the rule a bank actually has: an account with a loan
  collecting against it does not close.
- A payment settling before it booked, because a settlement date past the anchor was clamped back
  to it. Fixed by leaving the payment in flight instead.
- 31 percent of card authorisations declining for want of funds, because the first generator had
  no income side at all.

## Deviations from the specification

Recorded as amendments on [spec 003](../specs/003-historical-load.md), each with its reasoning:

- The identity probe resolved in favour of the existing primary keys.
- `ref.payment_statuses` gained `is_posted`; `core.gl_transactions` gained a polymorphic source
  reference with `ref.gl_source_entities` as its vocabulary; `core.transactions` gained
  `is_card_present`.
- No inter-regional interchange rate is seeded, and `ref.interchange_rates` gained no presentment
  dimension. The 2019 Commission commitments cap the inbound corridor and Nordbank is an EEA
  issuer, so the figures have no correct row to land on.
- Invariant 9's statistical band is suspended at `ci` rather than widened.
- Invariant 14 asserts Benford on card purchases only, by mean absolute deviation.
- Invariant 13 was replaced by a login-to-transaction coverage share, because the original
  required an unrealism to satisfy.

Spec 002 carries two amendments of its own: design rule 4 is refined, because the trigger is
`before update` only and a silent loader would stamp five years of history with the load
timestamp; and the two columns spec 003 added to the schema it delivered are recorded there.

## Verification

```bash
make up && make schema-apply
NORDBANK_ENV=ci NORDBANK_ANCHOR_DATE=2026-09-18 make seed
NORDBANK_ENV=ci NORDBANK_ANCHOR_DATE=2026-09-18 make seed-verify
NORDBANK_ENV=ci NORDBANK_ANCHOR_DATE=2026-09-18 make seed-manifest CHECK=1
make test && make test-dags && make test-integration
```

## What M3 inherits

A valid starting state for the mutation engine. Every audit timestamp is the row's true
last-change time in simulated history, no row carries the load timestamp, and every identity
sequence is synchronised past the largest key its table holds. The runbook documents the pattern
M4 needs: load with an anchor in the past, then step forward, so a backfill has genuine
day-by-day change to extract.
