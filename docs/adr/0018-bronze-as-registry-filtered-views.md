# 0018 — Bronze is a view over the lake that reads only registered batches

Status: Accepted
Date: 2026-09-30

## Context

Specifications 005 and 006 land fifty entities as Parquet under
`bronze/<source>/<entity>/ingest_date=/batch_id=/`, register every batch in `ops.batch_registry`,
and leave the objects of a failed or interrupted run in the bucket by design (ADR 0008): a run
that dies after writing and before registering leaves files that look exactly like good data.
Specification 007 gives dbt a bronze model per entity, and something has to decide what those
models are and how they read.

Four measurements, taken at specification 007's planning on DuckDB 1.5.5 and the fourth
acceptance run's lake, constrained the answer:

- **The files do not share one schema.** The writer inferred Parquet types from values, so 13 of
  the 49 entities with objects carried between two and seven physical schemas; only settlements
  differed because of a contract version. Specification 007 made the writer apply the contract's
  schema, and the history written before it remains.
- **DuckDB matches columns by name, and without `union_by_name` the first file decides.** A later
  file missing a column fails the read, and a column absent from the first file is silently
  dropped. Four of the thirteen entities failed; nine were right by the luck of which file sorted
  first. With `union_by_name` all thirteen read, at the widest type, with no value changed.
- **A glob that matches nothing fails**, when the view is created and whenever it is queried, and
  no SQL construct falls back from it. A table function cannot take a subquery, so the file list
  cannot come from the registry at query time either.
- **A view re-lists its prefix on every query.** An object planted after the view was created
  appeared in the next count, over S3 and on a local disk.

## Decision

Each bronze model is a view, generated from its entity's contract, and every one reads the lake
through one macro, `bronze_read`:

- it reads every object under the entity's prefix by column name (`union_by_name`) and casts
  every column to the type the contract's latest version carrying it declares;
- it keeps a row only when the **`batch_id` of the object key** it came from is registered in
  `ops.batch_registry`;
- an entity with no registered batch that landed rows builds as a typed empty relation, decided
  when the view is built, because its glob could not be read.

`bronze_guard`, a hook that runs before any model, fails the build if a bronze model is anything
but its generated call to the macro, or if any other model reads the lake itself. `transform_bronze`
runs `dbt build` whenever an ingestion registers a batch, which tests every landing and rebuilds
a view that was built empty.

## Consequences

- Bronze holds no data of its own. There is one copy of every landed row, the object in the lake,
  and the warehouse file stays the size of the platform's state rather than of its history.
- **Every read lists the prefix and reads every object's footer.** Measured at `ci` over the
  fourth acceptance run's sixty-one days: all 49 entities read through the filter in 2.46 s and
  3,017 HTTP requests over 2,968 objects, one listing request per entity and one read per object,
  against a local MinIO. The object count grows with days of ingestion, about one object per
  entity per day, not with the profile, so `dev` lists the same number of objects as `ci` over the
  same window and reads larger ones. For `full`, projected rather than measured: a year of daily
  runs is about 18,000 objects, which at the measured 0.8 ms per object is about 15 seconds per
  full bronze read against MinIO on the same machine, and against a cloud object store, at tens of
  milliseconds a request, minutes. Compaction and retention, M7's, are what bound it.
- **A view built empty is stale until its entity's first rows land and the view is rebuilt.**
  Between the registration and `transform_bronze`'s next run it reads nothing. The FRED feed,
  which has no key in this deployment, is such a view.
- A column no object carries yet, such as one a new contract version adds before its first
  delivery, is selected as a typed null, also decided at build, and becomes real at the rebuild
  after its first delivery.
- Reading a bronze view needs `httpfs` and the lake's credentials in the reading connection, not
  only the warehouse file. dbt's profile supplies both; any other reader must do the same.
- A view over S3 Parquet is DuckDB's. The BigQuery target at M10 has to express bronze another
  way, as an external table or a load, and this record does not decide which.

## Alternatives considered

**Materialise bronze as tables in DuckDB.** Every read would be local and fast, and the listing
cost above would be paid once per load. Rejected because it is a second copy of the lake: the
warehouse file would grow with the whole history, every landed row would exist twice, and the
two copies could disagree, which is the thing the registered-batch filter exists to prevent. It
would also make the warehouse, which is single-writer, the bottleneck for every landing.

**Read the lake unfiltered.** The simplest view, and the one a glob gives for free. Rejected:
the objects of a failed or interrupted run are in the bucket by design and look complete, so an
unfiltered model reads the output of runs that died and nothing anywhere complains (ADR 0008).

**Filter on the file's `_batch_id` column.** Every record carries it, so it looks like the natural
key. Rejected by measurement: a byte-for-byte copy of a registered object planted under a new
key passed that filter, 258 rows in the probe, because the copy's records still name the
registered batch. The object key is what the registry's registration covers, so the key is what
the filter reads, and a test asserts the key and the column agree for every row the filter keeps.

**Take the file list from the registry.** Reading only the registered objects would skip listing
and never touch an orphan. Rejected because DuckDB cannot: a table function's arguments cannot
contain a subquery, so the list would have to be baked into the view at build time, and every
landing would then need a rebuild before it was readable.
