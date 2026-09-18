# 0011 — COPY with explicit identity keys, and what the load has to switch off

Status: Accepted
Date: 2026-09-18

## Context

Spec 003 loads sixteen `core` tables at three scales, the largest being tens of millions of
rows, and requires the result to be byte-identical across runs of the same seed. Two things had
to be established before the loader could be designed, and both were measured rather than
reasoned about.

The first was whether `COPY` can supply a primary key at all. Every `core` primary key is
`bigint generated always as identity` under spec 002 design rule 6, and `generated always` is
the form that refuses an explicit value. A deterministic generator has to assign its own keys:
if the database assigns them, the values depend on insertion order and on whatever the sequence
happens to hold, and the run is no longer a function of the seed.

The second was what the load costs with the schema's constraints in place. Design rule 12 puts
an index on every foreign key column, `core.transactions` carries nine foreign keys, and
`core.gl_entries` carries a deferred constraint trigger that ADR 0009 measured under bulk
`COPY`. That ADR's conclusion — commit GL in chunks of about 100,000 entry rows — was the
starting point, not a conclusion to inherit.

## Decision

**Primary keys stay `generated always as identity`, and `COPY` supplies them.**

Measured on PostgreSQL 16.15:

| Attempt | Result |
|---|---|
| `COPY t (id, payload) FROM STDIN` into `generated always` | accepted |
| `INSERT (id, payload) VALUES (...)` into the same column | refused, `cannot insert a non-DEFAULT value into column "id"` |
| `COPY ... OVERRIDING SYSTEM VALUE FROM STDIN` | syntax error; `COPY` has no such clause |
| sequence after `COPY` supplied the keys | unmoved: `last_value` 1, `is_called` false, `max(id)` 300 |
| an identity insert afterwards, without `setval` | duplicate key on the primary key |
| a NULL in the identity column under `COPY` | not-null violation, not a fallback to the default |

`COPY` therefore does what the loader needs while `INSERT` keeps refusing what design rule 6
was written to refuse. The rule is unamended.

Two consequences are load-bearing:

- **`setval` on every sequence after the load is mandatory.** `COPY` does not advance the
  identity sequence, so without it the first row the M3 mutation engine inserts collides on the
  primary key. The loader does it for all sixteen tables and asserts the result.
- **The generator assigns every key of every row.** There is no mixed mode where some rows
  carry a key and others fall through to the default, because a NULL in that column is a
  not-null violation rather than a default.

**Foreign keys are dropped for the duration of the load and revalidated afterwards. Secondary
indexes are not.** Measured on `core.transactions` with its nine foreign keys and ten secondary
indexes, loading 1,000,000 rows against 25,000 parent accounts:

| Path | Drop | Load | Restore | Total |
|---|---|---|---|---|
| Everything in place | — | 35.77 s | — | 35.77 s |
| Foreign keys dropped | 0.56 s | 13.27 s | 1.82 s | 15.65 s |
| Secondary indexes dropped | 0.62 s | 29.80 s | 2.32 s | 32.74 s |

Foreign key validation costs 63 percent of the baseline load, and revalidating the whole table
afterwards costs 1.82 seconds because it is one join against small parent tables rather than
ten million index probes. Dropping the secondary indexes saves 3.03 seconds of 35.77, which is
8 percent, and buys a window in which the table is unqueryable for no useful return. So the
loader drops one and keeps the other.

**The general ledger is committed in chunks of 100,000 entry rows, and every chunk runs
`ANALYZE core.gl_entries` inside its own transaction before committing.** The `ANALYZE` is not
housekeeping. It is the difference between a linear commit and a quadratic one:

| Entry rows in one transaction | Commit without `ANALYZE` | Commit with `ANALYZE` |
|---|---|---|
| 20,000 | 18.37 s | — |
| 40,000 | 74.07 s | — |
| 50,000 | — | 0.28 s |
| 80,000 | 300.47 s | — |
| 100,000 | — | 0.50 s |
| 200,000 | — | 1.02 s |
| 400,000 | — | 2.14 s |
| 800,000 | — | 4.35 s |

Quadrupling at every doubling on the left, linear on the right.

The cause is the trigger function's cached plan. `core.assert_gl_batch_balanced` runs

```sql
select e.entry_currency_code, sum(e.amount)
  from core.gl_entries e
 where e.gl_transaction_id = v_gl_transaction_id
 group by e.entry_currency_code having sum(e.amount) <> 0 limit 1
```

and on a freshly truncated table, with no statistics, the planner chooses an index scan over
`ix_gl_entries_entry_currency_code` — a column with one distinct value in this book — and
applies `gl_transaction_id` as a filter. That reads the whole table once per firing, and the
trigger fires once per row. plpgsql caches the plan, so the choice is made once and paid for
every time. With statistics present the planner uses `ix_gl_entries_gl_transaction_id` and the
commit is linear.

The deferred queue, which ADR 0009 identified as the constraint, is not the binding one. It
measures 13.11 bytes per entry row here against that ADR's 12.58, so the 512 MiB container
limit would permit roughly 40 million entry rows in a single transaction. **The chunk size is
set by the plan, not by the memory.** 100,000 is retained because it bounds the queue at 1.3 MB
and because throughput is flat above it — 27,800 rows per second at 100,000 against 31,000 at
800,000 — so there is nothing to buy by growing it.

## Consequences

**A forgotten `setval` is invisible until M3.** The load succeeds, every invariant passes, the
manifest hashes match, and the failure arrives in a different milestone as a duplicate key on
the first mutation. The loader therefore treats sequence synchronisation as part of the load
rather than as a tidy-up, and asserts afterwards that every sequence exceeds the maximum key in
its table.

**The load holds a window in which the database has no foreign keys.** For as long as it runs,
`core` will accept an orphan. The load is single-user and revalidates before it reports success,
and a failure mid-load leaves the constraints off — which is visible, because `make seed-verify`
invariant 12 asserts from the catalogue that all of them exist and are validated. That invariant
was specified to catch exactly this, and it now has a real thing to catch rather than a
hypothetical one.

**The `ANALYZE` requirement is a property of the trigger, not of this loader.** Anything that
bulk-loads `core.gl_entries` into a table without statistics inherits the quadratic commit,
including the M3 mutation engine after a nuke. The finding belongs to
`core.assert_gl_batch_balanced` and is recorded here because this is where it was measured.

**The determinism guarantee is tied to the standard library and deliberately not to a
dependency.** The generator draws from `random.Random` rather than from a third-party generator.
A determinism guarantee that a routine dependency bump can break is not a guarantee, and the
committed `ci` manifest makes it a check that CI enforces: a library that reserves the right to
change its stream between versions would break a committed hash for a change that touched no
generator code. Measured throughput is 26.7 million uniform draws per second and 2.8 million
log-normal, which is ample at every profile, so nothing is being traded away for this.

## Alternatives considered

**Relaxing the primary keys to `generated by default as identity`.** This was the specification
author's stated lean, and it lost on the probe. `COPY` already accepts explicit values into
`generated always`, so the change would buy no capability the loader lacks, while giving up the
guarantee that application code cannot supply a key by accident — the guarantee design rule 6
exists for. It is a strict loss once the measurement is in.

**Staging tables plus `INSERT ... OVERRIDING SYSTEM VALUE`.** Rejected for the same reason and
a worse cost: every row would be written twice, once into the staging table and once into the
real one, doubling the write volume and the disk at the `full` profile to work around a
restriction that does not apply.

**Keeping the foreign keys and accepting the load cost.** Rejected on the measurement: it is
2.3 times slower, which at twenty million transactions is the difference between roughly five
minutes and twelve. It is the honest default and would be the right answer if revalidation were
expensive, but revalidation is 1.82 seconds per million rows because the parents are small.

**Dropping the secondary indexes as well.** Rejected on the measurement: 8 percent. ADR 0009's
partitioning entry records a similar shape of reasoning — an optimisation that prunes a query
pattern the platform does not run is not an optimisation — and this is the same judgement
applied to a write path.

**Raising the GL chunk size now that the queue is known to permit it.** Rejected. Throughput is
flat above 100,000 entry rows, so the only thing a larger chunk buys is a larger queue and a
longer transaction to lose on failure.
