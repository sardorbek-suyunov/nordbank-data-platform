# 0008 — Bronze immutability by key construction

Status: Accepted
Date: 2026-09-18

## Context

Bronze is the immutable record of what a source sent (ADR 0001). Everything downstream can be
rebuilt because that record cannot change. The question is what enforces it.

Object storage will happily overwrite a key. A rerun that writes to the same path as a previous
run destroys history silently: the object store reports success, the file looks complete, and
the only evidence that yesterday's data is gone is that a number moved.

## Decision

The batch id is part of the object key:

```
bronze/<source>/<entity>/ingest_date=YYYY-MM-DD/batch_id=<batch_id>/part-NNNN.parquet
```

A batch id is unique per extraction run, so two writes cannot address the same key. Overwriting
is not a thing the platform declines to do; it is a thing it cannot express. Immutability
becomes a property of the naming scheme, enforced by arithmetic rather than by a bucket setting,
a policy or a habit.

## Consequences

There are more objects, and they are smaller. A daily run per entity multiplies by however many
batches a day produces, and the `full` profile makes that a real number. A compaction and
retention story is required before that profile is usable: something has to roll old batches
into fewer, larger files and decide how long raw batches are kept. That work is not in this
milestone, and pretending it is optional would be a mistake.

Every bronze model must filter to batch ids registered as successful in `ops`. A run that dies
after writing objects but before registering leaves files behind that look exactly like good
data. Without the filter, a model reads the output of a failed run and nothing anywhere
complains. This makes the batch registry load-bearing: if it is wrong, bronze is wrong.

Orphaned objects from failed runs accumulate and nothing removes them yet. A reaper belongs
with the operational work at M7, and until then storage grows monotonically with every failure.

Listing costs rise with object count, which is cheap on MinIO locally and is a real cost
against a cloud object store, which matters at M10.

## Alternatives considered

**Bucket versioning.** The obvious answer, and the one originally specified. Rejected because
MinIO implements versioning on the erasure-coded backend, which means a single-node multi-drive
deployment: four volumes instead of one, and roughly double the storage footprint at the `full`
profile, to buy a guarantee the key scheme provides for nothing. It is also vendor-specific
behaviour in the one layer of the platform that should stay portable, and the project has
already had to move MinIO between registries once.

**Convention only: do not overwrite.** Rejected because it is unenforceable. It holds until a
retry, a backfill with a hardcoded path, or a bug that reuses a batch id, and its failure mode
is silent data loss rather than an error. A rule that depends on everyone remembering it is not
a guarantee, and bronze immutability is the assumption every other layer rests on.

**Write-once bucket policy or object locking.** Rejected for the same reason as versioning:
it needs the erasure-coded backend and a retention configuration, and it turns an accidental
rerun into an unrecoverable bucket state rather than a harmless extra key.
