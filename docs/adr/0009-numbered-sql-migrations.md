# 0009 — Numbered SQL over a migration framework

Status: Accepted
Date: 2026-09-18

## Context

The source system needs a schema: forty-five tables across `core`, `ref` and `platform`, with
constraints, indexes, triggers and reference seeds. Something has to apply it, and something
has to decide what happens when it changes.

The reflex answer is a migration framework. Alembic or Flyway would give versioned, ordered,
checksummed migrations with a recorded history and a rollback path, and both are the right
answer for a production database that must move forward without losing data.

This database is not that. It is a generated fixture: `make nuke` deletes it, `make up`
rebuilds it in under forty seconds, and `make schema-apply` puts the schema back. Nothing in it
is precious, because everything in it is either DDL in this repository or data the generator
will produce again. The question is not how to migrate it but how to make it readable and
re-appliable.

## Decision

DDL lives in numbered, idempotent SQL files under `infra/docker/postgres-source/schema/`,
applied in order by `make schema-apply`. Seeds live alongside them under `seed/`, grouped by
domain, each statement using `on conflict ... do update ... where` the incoming values differ.

Idempotency is a property of every file: `create table if not exists`, `create or replace
function`, `create index` guarded by a catalogue lookup, and `drop trigger if exists` before
`create trigger`. Applying twice changes nothing, which is proven rather than asserted: dump
the schema, apply again, dump again, diff.

Three rules are applied by looping over the catalogue rather than by writing them out per
table: the audit columns, the `updated_at` index and trigger, and an index on every foreign key
column. Sixteen hand-written trigger pairs and sixty-six hand-written foreign key indexes are
sixteen and sixty-six chances to forget one on the next table, and these are invariants rather
than per-table choices.

## Consequences

**There is no rollback path.** A migration framework can step a schema back; this cannot. The
mitigation is that the database is rebuilt rather than migrated: the recovery from a bad DDL
change is `make nuke && make up && make schema-apply`, which costs under a minute. That is an
acceptable answer here and would not be in production, and the difference is worth being
explicit about rather than discovering later.

**There is no drift detection against a shared environment.** A framework records which
migrations an environment has applied; numbered files do not. The mitigation is
`make schema-check`, which compares the live schema against `docs/data_dictionary.md` in both
directions and is wired into `make test-integration`. It catches a column added to the database
without being documented, and a column documented that does not exist. It does not catch a
developer running an older checkout against a newer database, and nothing here does.

**`schema-apply` is not purely "run the files in order".** `platform.column_classifications` is
generated from `docs/data_dictionary.md` rather than from a seed file, because a hand-written
classification seed would be a second copy of what the dictionary already says, and the whole
reason the table exists is that a classification held only in prose drifts from the code that
depends on it. The cost is that the apply step is a script rather than a loop over psql, and
that a malformed dictionary aborts the apply. That is the right failure: a partial
classification load means the extraction layer at M4 silently fails to tokenise something.

**The DDL is readable, which was the point.** A reviewer opens
`infra/docker/postgres-source/schema/35_core_ledger.sql` and reads the ledger, with the
constraint that makes it double-entry directly beneath the table it constrains. No framework
sits between the reviewer and the statement.

## Alternatives considered

**Alembic.** Rejected. It is Python-native and already in the dependency tree's reach, but its
migrations are generated diffs: a reviewer reads `op.add_column(...)` rather than the DDL, and
autogenerate against a declarative model would mean maintaining SQLAlchemy models of a schema
that has no application behind it. The rollback path it buys is worth nothing on a database
that is rebuilt rather than migrated.

**Flyway.** Rejected for the same reason plus a heavier one: it is a JVM tool, and adding a
Java runtime to the stack to order a dozen SQL files is a poor trade on a project already
running nine services against a measured memory floor.

**Applying the DDL from the container entrypoint.** Rejected. Postgres runs
`/docker-entrypoint-initdb.d` once, on an empty data volume, so the schema could only be
changed by destroying the database. It would also make the schema invisible to anyone who did
not know the entrypoint convention, and it would give no way to re-apply after an edit.

## The privilege that made the platform schema possible

`platform` did not exist at M1, and the application role could not create it: `CREATE` on a
database is held by the database owner and not by `PUBLIC`, and the role owns two schemas
rather than the database. One line in `infra/docker/postgres-source/init/10_privileges.sh`
closes that:

```sql
grant create on database nordbank to nordbank_app;
```

This is a deliberate widening and is recorded as one rather than left incidental. It takes the
application role from owning two named schemas to being able to create **any** schema in the
database. That is acceptable for a role that already owns every object the platform writes,
and whose credentials never leave the loader; extraction authenticates as `nordbank_reader`,
which holds `SELECT` and nothing else and is refused `CREATE` in every schema. The grant does
not widen what the extraction path can do, which is the boundary that matters.

**The alternative, rejected.** Create `platform` in M1's superuser init script alongside `core`
and `ref`. It needs no new privilege at all, which is the argument for it. It loses because it
would put knowledge of a schema introduced at M2 into a milestone that predates it: a reader
of spec 001 would find a schema that specification never mentions, and the M1 provisioning
would have to be edited every time a later milestone adds a schema. Granting the owner role
the privilege to own what it creates keeps each milestone's schemas in that milestone's DDL.

**The negative consequence.** An init script runs once, on an empty data volume, so an existing
database does not gain the grant and `make schema-apply` fails against it with `permission
denied for database nordbank`. The recovery is `make nuke && make up`, which is the path
acceptance criterion 1 specifies anyway, but it is a real trap for anyone applying this branch
to a stack started before it.

## The deferred balance trigger, measured

Section 4 of spec 002 requires a `deferrable initially deferred` constraint trigger on
`core.gl_entries`. Its cost under bulk `COPY` was measured before the specification was
reissued, because the M3 historical load inserts millions of rows and an unusable trigger
discovered during that load would be discovered too late.

Measured on `postgres-source`, PostgreSQL 16.15 with `mem_limit: 512m`, loading 1,000,000 entry
rows in 250,000 four-line batches:

| Path | COPY | COMMIT | Total | After-trigger queue |
|---|---|---|---|---|
| No trigger, baseline | 4.44 s | 0.02 s | 4.46 s | — |
| Deferred per-row constraint trigger, one transaction | 5.27 s | 6.19 s | 11.46 s | 12.58 MB |
| Deferred per-row constraint trigger, ten 100k-row transactions | 5.8 s | 5.8 s | 11.6 s | 1.26 MB peak |
| Statement-level trigger with `referencing new table` | 5.12 s | 0.002 s | 5.12 s | none |

Row-level `after` triggers do fire on `COPY`, and deferred events queue at 12.6 bytes per row in
`AfterTriggerEvents`, a backend memory context that does not spill to disk. Against the 512 MiB
container limit that is about 126 MB of queue for 10M entries, and a single-transaction load
crosses the limit somewhere between 30M and 40M rows, where the backend is killed rather than
slowed.

**The constraint this places on M3.** The historical load and the mutation engine commit GL in
chunks of whole balanced batches. 100,000 entry rows per transaction is the measured figure: it
costs nothing in throughput against one large transaction and bounds the queue at about
1.26 MB. This is a constraint on the loader, not a setting it may ignore.

**The rejected alternative, and why it lost.** A statement-level trigger with a transition table
is 2.2x faster and queues nothing. Postgres requires a constraint trigger to be `for each row`,
so a statement-level trigger cannot be deferred, and it therefore validates at the end of each
statement. Measured consequence: a legitimate two-statement batch — insert the debit, insert the
credit, commit — is rejected at the first statement with `gl batch 9100001 does not balance`. It
forbids the normal way a posting batch is assembled, so it cannot be the enforcement mechanism
whatever it costs.

## Partitioning, decided against

`core.transactions` holds tens of millions of rows on the `full` profile, which makes
declarative monthly partitioning on `booked_at` an obvious candidate. It is decided against for
now, and the reasoning is recorded so that it is visible rather than implicit.

Extraction reads `updated_at`, not the business date. Partitioning on `booked_at` would prune
nothing on the access path the platform actually uses, so every incremental run would touch
every partition. It would add a partition maintenance job, make the foreign keys from
`fraud_alerts` and `gl_entries` harder to express, and complicate the DDL a reviewer is meant to
read, in exchange for pruning a query pattern the platform does not run.

**The condition that reverses it:** M3 measuring `full` profile load times or autovacuum times
on `core.transactions` that are unacceptable. If that happens, partitioning is reconsidered on
that evidence, and the candidate key is `booked_at` monthly with the extraction index kept on
`updated_at`. This reasoning carries into the M2 checkpoint when M2 closes.
