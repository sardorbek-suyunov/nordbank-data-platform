# 0016 — The contract in force is chosen by when the sender produced the delivery

Status: Accepted
Date: 2026-09-25
Supersedes: ADR 0014, for how a contract version is selected. Its other decisions stand: the
authored `in_force_from`, superseded versions kept under `history/`, the two times in
`meta.contract_version`, and the refusal of a recorded version whose file has changed.

## Context

ADR 0014 selected the contract for a batch as the highest version whose `in_force_from` is on or
before the batch's interval. For the relational source, the API feeds and the snapshot, the
interval starts on the day the delivery was produced: a core banking batch is keyed on the run's
logical date and read that day, an FX or FRED batch on the run's logical date, a sanctions
snapshot on the publisher's export time. For a clearing file it does not. A file's batch is
keyed on the settlement date the file covers, and a late file covers a date days before the one
on which it was sent.

A sender changes its format at a point in its own time, which is when it sends. So a late file
for a date before a format change, sent after it, carries the new layout, and ADR 0014 read it
against the old contract. Measured in a scratch run at specification 006's review, with the
real open, extract and register steps: a file for settlement date 2026-09-01, delivered on
2026-09-04 without `merchant_name`, failed on breaking drift. It failed again on 2026-09-05 and
2026-09-06, allocating a new registry sequence each day, and no contract bump could release it:
selection by settlement date always chose version 1 for 2026-09-01, and moving version 2's
`in_force_from` back to 2026-09-01 would have broken the replay of the on-time files for
2026-09-01 and 2026-09-02, which really were in the old layout.

None of the second acceptance run's 132 clearing batches would have selected differently under
either rule; no late file there straddled a change. The trap was real and unexercised.

## Decision

The contract in force for a delivery is the highest version whose `in_force_from` is **on or
before the day the sender produced the delivery, as the platform observes it**. The comparison
is `in_force_from <= that day`, against the date only.

| Mode | The day the platform observes the delivery was produced |
|---|---|
| Relational | The run's logical date, which is the interval start |
| API, interval | The run's logical date, which is the interval start: an API that has changed shape answers in its new shape whatever date it is asked about |
| File | The ingest date: the logical date of the run that finds the file |
| Snapshot | The publisher's export time, which is the interval start |

Only the file mode changes behaviour. A clearing file's batch keeps its settlement-date key, so
the batch arithmetic and the reconciliation still group by settlement date; only the contract it
is read against moves.

**A delivery refused with a verdict is parked.** A verdict is a refusal the same bytes will
earn again against the same contracts: breaking drift, a structurally malformed file, a
declaration conflict. Attempting it every run allocated a new registry sequence each day and
failed every morning. A parked delivery allocates nothing: each run records a `parked`
sighting naming the batch that refused it, until the fingerprints of the contracts in force for
it change, when it is attempted once more and sighted `reattempted`. The parked state is not
stored: it is derived from the delivery's latest attempt in `ops.file_sighting`, that batch's
status and reason in `ops.batch_registry`, and the fingerprints of the versions it was read
against in `meta.contract_version`. A parked delivery has no `ops.ingested_file` row, because it
did not land, and a renamed copy of it is the same parked delivery, because identity is the
checksum.

## Consequences

- A late file that straddles a format change is read against the layout it was sent in. The
  simulated processor now builds each file in the layout, and stamps its header with the
  production time, of the day it sends it, and the acceptance history carries a scripted
  straddling file: settlement date 2026-09-01, sent 2026-09-04.
- **A file delivered on day X and ingested on X+1, across a version bump, is read one version too
  new.** The ingest date is the platform's observation of when the file was sent, and it is late
  by as much as the run that finds the file is late. The backfill never produces this, because
  it delivers and ingests on the same day. A deployed platform would, when a day's run fails or
  is skipped on the day of a bump. The recovery is parking plus a contract bump: the file parks
  instead of failing every morning, and is attempted again when the contracts in force for it
  change. A bump that releases it must accept its layout on the day it is next attempted, which
  for a file one version too new means a version that accepts both layouts or the sender sending
  again; the object store's arrival time would remove the error, and is a real-time clock the
  simulation cannot use.
- A parked delivery stays visible every day as a `parked` sighting and a failed batch, and the
  run that first refused it fails, which pages. The later runs do not fail: a delivery the
  platform has already refused is not news. The daily sightings grow `ops.file_sighting` like
  every other sighting (M7's retention).
- Parking needs no table of its own, and the price is a query: deciding whether a delivery is
  parked joins three tables per candidate in the open step. At one file a day that is nothing.

Two consequences of ADR 0014's own decisions were found after implementation depended on it.
ADR 0014 is superseded and is not edited beyond its status, so they are recorded here:

- **Specification 007 moves the CI anchor to `ACCEPTANCE_ANCHOR`**, retiring the second anchor
  CI seeds at. The contracts are authored for one anchor (ADR 0014), and a CI job that ingests
  must seed at it.
- **Nuke-and-rebuild as the warehouse migration holds only because the source replays all of
  history.** ADR 0014 made `make nuke` and a fresh backfill the migration for a changed
  `meta.contract_version`, and that is sound only while every row can be ingested again. A
  deployed warehouse holds history no source will send twice, and needs numbered, idempotent
  migrations like ADR 0009's for the source schema. M7 owns them.

## Alternatives considered

**Keep selection by interval for every mode, and resolve a straddling file by hand.** Rejected
by the measurement above: there is no hand resolution. The file cannot be accepted by a bump,
and editing a recorded version is refused, correctly, because it would change what a replay
accepts.

**Select a file's contract by the header's `created_at`, the sender's own statement of when it
produced the file.** Closer to the truth than the ingest date, and immune to the day-late run.
Rejected because the header is part of the format being selected for: a changed layout can move
or rename the field the selection would read, and a declaration is trusted only as an attribute,
never to decide how the bytes around it are read (ADR 0013). The ingest date is the platform's
own observation.

**Try every version and accept the first that reads the file.** Tolerates any straddle and any
late run. Rejected because it removes the control: a file that drifted would be accepted by
whichever old version it happened to fit, and breaking drift would never be seen.

**Store the parked state in `ops.ingested_file` or a table of its own.** Rejected: `ingested_file`
means "landed", and a parked file must not count as landed; a table of its own would restate what
the sightings, the registry and the contract versions already record, and could disagree with
them.
