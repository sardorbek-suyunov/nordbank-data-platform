# 0013 — A delivered file is identified by the checksum of its content

Status: Accepted
Date: 2026-09-23

## Context

Specification 006 adds two ingestion modes in which the unit of work is a delivered object
rather than a window over a table: the card settlement files a processor sends, and the sanctions
snapshot a publisher releases. Specification 005's batch identity answers "has this interval
been registered", and nothing in it answers "has this file been landed", which is what a file
feed has to know. Three situations have to come out right:

- the same file is processed twice, because a run was re-triggered or a day was re-run;
- a file is delivered again under a different name with the same content, which a sender does
  when it re-sends to be safe;
- a sender corrects a file and re-sends it under the same name.

The first two must land nothing. The third must land, because it is a different file.

Two further facts shaped the decision. Deliveries arrive with identifiers in the clear, so the
drop zone cannot be the lake. And the sanctions publisher, measured, exports four times a day
under a new version string each time, whether or not the content changed.

## Decision

A delivered object is identified by the SHA-256 of its bytes. `ops.ingested_file` holds one row
per content that has landed, keyed on that checksum, with the batch it landed as, and is written
by the register step when that batch registers — so a file whose batch failed is not ingested
and is picked up again once the reason is resolved. `ops.file_sighting` records every time
discovery saw an object and what it concluded, which is the evidence that a renamed copy was
recognised rather than merely not duplicated.

Discovery lists and hashes outside the warehouse pool; the comparison against what has landed
and the record of the sighting happen in the pooled open step.

A snapshot's version string is recorded beside the checksum but is not its identity. A new
version string over unchanged content is a no-op.

Deliveries arrive in a separate bucket, `nordbank-inbound`, which stands for the third party's
side of the boundary in the way `postgres-source` does for the core banking system. The lake
holds only what ingestion wrote.

## Consequences

- Reprocessing is idempotent by construction: a known checksum lands nothing and records a
  sighting naming the batch it already landed as.
- A corrected re-send lands as a new batch, and the correction is visible downstream as two
  batches for one settlement date rather than as an overwrite.
- **Two genuinely different deliveries with identical bytes are one file.** For the settlement
  feed that would be two empty days, each file being a header, a column line and a trailer with
  nothing between them. It is prevented by the file rather than by the platform: the header
  record carries the settlement date and a sequence number, so no two days' files can be
  byte-identical. A feed whose format carried neither would need a different identity.
- Discovery reads every object in the inbound prefix on every run to hash it. At one file a day
  that is nothing; a feed with thousands of objects a day would want the checksum from the
  object store's metadata, which MinIO's ETag does not reliably provide for multipart uploads.
- The inbound bucket holds cleartext identifiers and is outside the vault's reach. It stands for
  a third party's system, and the retention of what a sender delivered is a question for M8's
  governance work rather than something this record settles.

## Alternatives considered

**Identity by object key.** Simple, and what a prefix-watching sensor gives for free. Rejected
because it is wrong in both directions the context lists: a renamed copy lands again, and a
corrected re-send under the old name is refused as a duplicate.

**Identity by the sender's own declared fields — settlement date and file sequence from the
header, or the snapshot's version string.** Closer to what the sender means, and it survives a
rename. Rejected as the identity, though kept as recorded attributes, because it trusts the
sender to change the declaration whenever the content changes. The measured sanctions publisher
changes its version string four times a day over content that has not changed, and a sender that
corrects a file without incrementing its sequence would have the correction refused.

**Landing deliveries under a prefix of the lake.** One bucket, one set of credentials. Rejected
because a delivery carries cleartext identifiers and the lake is the one place ADR 0005 says must
never hold one; a prefix convention is a discipline, and a bucket is a boundary.
