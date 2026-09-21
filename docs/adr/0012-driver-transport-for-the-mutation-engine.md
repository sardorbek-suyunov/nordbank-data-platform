# 0012 — A driver for the mutation engine, and psql for everything that reads a mounted file

Status: Accepted
Date: 2026-09-20
Corrected: 2026-09-21. This record said the committed default in `.env.example` stays at
5432 and that only the local `.env` was moved. That is wrong: the committed default was
already 55432 when this was written, and `docs/runbook.md` explained it. The default is
15432 from M4, for a reason measured then and recorded in the runbook: 55432 is inside the
Windows dynamic port range, so any application's loopback connection can hold it and the
container then comes up with no published port at all. The transport decision is unchanged.

## Context

Every host-side tool in this repository reaches the source database the same way: `psql`,
inside the container, through `docker compose exec`. `scripts/source_db_exec.py` is the single
place that knows how, and its docstring states the reason — "The host has no Postgres driver
and does not need one."

That was true and it was also a choice, recorded as though it were a property of the machine.
`docs/project_state.md` and `generator/writer.py` both repeat it. Until M3 nothing depended on
the difference: `make schema-apply` applies files, `make schema-check` runs read-only queries,
and the loader streams `COPY` payloads one way with nothing to read back.

M3 is where it stops being free. A tick reads the state a day's decisions need, decides in
Python, then writes the result as one transaction that either commits whole or leaves nothing
behind. Spec 004's acceptance criterion 2 is written in terms of that transaction. Over a pipe
this needs a request-and-response protocol: a sentinel to frame each reply, a reader thread per
stream so a pipe nobody drains cannot deadlock, a hard timeout on every read, and detection of
`psql` exiting under `ON_ERROR_STOP` — because the failure mode of a protocol over a pipe is a
hang, not an error.

So the transport was measured before anything was built on it.

## Decision

**The mutation engine reaches the source database with psycopg 3. Everything else keeps psql.**

Measured on PostgreSQL 16.15, on the workload a tick actually performs: read the state a day
needs (17,907 rows across four queries), then one transaction copying 10,403 rows over five
tables and folding 1,824 account balances. Interleaved, three rounds each:

| Transport | connect | state read | write transaction | total |
|---|---|---|---|---|
| Held-open `psql` session | 71 ms | 298–386 ms | 306–353 ms | 628–765 ms |
| psycopg 3 | about 60 ms | 31–40 ms | 464–523 ms | 520–561 ms |

The write path isolated, with the process launch counted, which is what a single `make tick`
pays, five rounds:

| | `psql` one-shot | psycopg, `str` | psycopg, pre-encoded bytes |
|---|---|---|---|
| range | 803–861 ms | 465–509 ms | 464–514 ms |

The driver is roughly ten times faster on the state read and about 1.7 times faster on a
single-tick write. Writing pre-encoded bytes in one-mebibyte blocks rather than a string made
no measurable difference, so the simple form is used.

Speed is not the main reason. The driver deletes the sentinel protocol, the reader threads, the
timeouts and the hang, and replaces them with `connection.rollback()`. Verified: an induced
`1/0` inside a tick-shaped transaction raised `psycopg.errors.DivisionByZero` as an ordinary
Python exception, left the connection usable, and left the row it had updated at its original
`2023-09-18` timestamp.

**The division of labour.** `make schema-apply` runs SQL files mounted at `/schema` inside the
container, which a host driver cannot open, and every other M2 target is built on
`source_db_exec`. `schema_contract.Executor` was written to abstract exactly this difference —
the host uses psql, the in-stack integration test uses a cursor — so the abstraction already
exists and this decision uses it rather than adding one.

**The connection proves where it landed.** `scripts/source_db_driver.py` asserts on open that
the server carries the three expected schemas, and reports when the major version is not the
16 everything was measured against. Every failure path names a port collision as a candidate
cause.

**One statement form changes.** `SET LOCAL nordbank.sim_now = %s` is a syntax error: `SET`
accepts no bind parameter. The driver uses `select set_config('nordbank.sim_now', %s, true)`,
the function form of `SET LOCAL`, which keeps the value parameterised rather than interpolated
into SQL.

## Consequences

**Two transports in one repository.** A reader now has to know which is which. The rule is one
sentence and is stated in both modules: the driver is for the mutation engine, psql is for
anything that applies a file from inside the container. This is the real cost of the decision
and it is not recovered by the measurements above.

**A runtime dependency where there was none.** The project depended on `pyyaml` and nothing
else. `psycopg[binary]` is a compiled wheel, so a platform without one falls back to building
`psycopg[c]` or to the pure-Python implementation. It is already present in the Airflow image
at 3.3.5, beside the psycopg2 2.9.13 the Postgres provider brings, so `ops_source_tick` needs
no image change — but the host environment now has a native dependency in it.

**The driver can be shadowed and psql cannot.** `docker compose exec` addresses the container
by name and cannot reach the wrong server. The driver goes through the published port, and on
the machine this was measured on that port did not reach the container at all: `docker compose
ps` reported `0.0.0.0:5432->5432/tcp` while a native PostgreSQL 18 Windows service owned
`0.0.0.0:5432` and `:::5432`. The stack came up healthy, `make health` passed, and the driver
reached the other server and failed authentication. Docker publishes no warning for this.

The mitigation is the identity assertion and the error text, not a claim that it will not
happen again. The committed default in `.env.example` is not 5432, and the sentence that stood here said
it was; see the correction above. It is a non-standard port precisely so that the collision
with a native PostgreSQL installation does not happen on most machines, and the identity
assertion covers the machines where any default would collide.

**A second Airflow connection.** `nordbank_source_db` authenticates as `nordbank_reader`, which
is SELECT-only by design, so the tick cannot use it. `ops_source_tick` needs its own connection
as the application role, which means Airflow can now write to the source database where before
it could only read. The DAG is paused by default, which bounds it, but the capability is new
and is stated rather than introduced quietly.

## Alternatives considered

**A held-open `psql` session with a sentinel protocol.** This was the design before the
measurement, and it is a real option: it needs no new dependency, it cannot be shadowed by a
port collision, and a session held open for the duration of `tick-to` amortises the process
launch across sixty ticks. It lost on two counts. It is slower on both paths, decisively so on
the state read, where framing 17,907 rows as text through psql's unaligned output and parsing
them back costs an order of magnitude more than the wire protocol. And its failure mode is
worse: when `psql` exits under `ON_ERROR_STOP` mid-protocol the reader blocks, so correctness
depends on a timeout on every read and on noticing process death, where the driver simply
raises. Spec 004 requires a test that kills the transport mid-protocol and asserts the tick
fails rather than hangs; with the driver that test reduces to asserting an exception
propagates.

**One `docker compose exec` per phase, the existing pattern.** Measured at 575 ms per round
trip, plus 530 ms for the `docker compose ps` that every target runs first. A `ci` tick would
spend 2,044 ms on transport and process launch before generating a row, against acceptance
criterion 13's two-second budget. Rejected on that arithmetic alone.

**Running the tick inside the container**, where psycopg2 already exists and no port is
published. This removes the collision risk entirely and was the strongest alternative. It was
rejected because the generator is not mounted into the Airflow services and mounting it would
put a second copy of the generation code path inside the image, where `make tick` could not
reach it without `docker compose exec` — reintroducing the process launch this decision exists
to remove, and splitting the generator's execution environment in two.
