"""A file is identified by its content, not its name or arrival (spec 006 section 1, ADR 0013).

The checksum is SHA-256 over the bytes as delivered. Two consequences are the point of it: the
same file reprocessed finds its checksum already recorded and lands nothing, and a file renamed
but unchanged is recognised as the file it was. A name, a path or an arrival time would get
both wrong: the renamed copy would land again, and a corrected re-send under the old name would
be mistaken for the original.

The price is that two genuinely different deliveries with identical bytes are one file. For the
settlement feed that is ruled out by the file itself, whose header record carries the settlement
date and a sequence number, so an empty day's file cannot be byte-identical to another's.

Discovery runs outside the warehouse pool: it lists the inbound prefix and hashes objects, which
reads the object store and nothing else. The comparison against what has landed, and the record
of every sighting, happen in the pooled open step.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass
from typing import Any

NEW = "new"
ALREADY_INGESTED = "already_ingested"
PARKED = "parked"
REATTEMPTED = "reattempted"

# The failure reasons that are a verdict on the delivery itself. The same bytes read against
# the same contracts reach the same verdict every time, so a delivery refused with one of these
# is parked rather than attempted again on every run (ADR 0016).
VERDICTS: tuple[str, ...] = ("breaking drift", "structurally malformed", "declaration conflict")


def is_verdict(reason: str | None) -> bool:
    return bool(reason) and str(reason).startswith(VERDICTS)


@dataclass(frozen=True)
class Candidate:
    """An object in the inbound prefix, identified by what it contains."""

    key: str
    checksum: str
    size: int

    def as_dict(self) -> dict:
        return {"key": self.key, "checksum": self.checksum, "size": self.size}


def checksum(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def list_objects(client: Any, bucket: str, prefix: str, suffix: str) -> list[str]:
    """Every key directly under a prefix that ends with a suffix, sorted.

    Directly under: a key with a further slash after the prefix belongs to something else, such
    as the simulation's own bookkeeping under `_simulation/`, and is not a delivery.
    """
    keys: list[str] = []
    token = None
    while True:
        arguments = {"Bucket": bucket, "Prefix": prefix}
        if token:
            arguments["ContinuationToken"] = token
        response = client.list_objects_v2(**arguments)
        for item in response.get("Contents", []):
            key = item["Key"]
            rest = key[len(prefix) :]
            if "/" not in rest and key.endswith(suffix):
                keys.append(key)
        token = response.get("NextContinuationToken")
        if not token:
            break
    return sorted(keys)


def read_object(client: Any, bucket: str, key: str) -> bytes:
    return client.get_object(Bucket=bucket, Key=key)["Body"].read()


def discover(client: Any, bucket: str, prefix: str, suffix: str) -> list[Candidate]:
    return [
        Candidate(key=key, checksum=checksum(body), size=len(body))
        for key in list_objects(client, bucket, prefix, suffix)
        for body in (read_object(client, bucket, key),)
    ]


def landed_as(connection: Any, checksums: list[str]) -> dict[str, str]:
    """For each checksum that has already landed, the batch it landed as."""
    if not checksums:
        return {}
    placeholders = ", ".join("?" for _ in checksums)
    rows = connection.execute(
        f"select content_checksum, batch_id from ops.ingested_file "  # noqa: S608 - placeholders
        f"where content_checksum in ({placeholders})",
        checksums,
    ).fetchall()
    return {checksum_: batch_id for checksum_, batch_id in rows}


@dataclass(frozen=True)
class Parked:
    """A delivery whose last attempt was refused with a verdict, and the contracts it met."""

    batch_id: str
    reason: str
    fingerprints: tuple[tuple[str, str], ...]


def parked(connection: Any, source_system: str, checksums: list[str]) -> dict[str, Parked]:
    """For each checksum that is parked, what parked it and against which contracts.

    **Derived, not stored.** A delivery is parked when the batch its most recent attempt
    allocated failed with a verdict. The attempt is its latest `new` or `reattempted` sighting,
    the verdict is on the registry row, and the contracts are the fingerprints of the versions
    that row and its sibling batches recorded, so the parked state has one source and cannot
    disagree with the registry. A parked delivery has no `ops.ingested_file` row: it did not
    land (ADR 0013).
    """
    if not checksums:
        return {}
    placeholders = ", ".join("?" for _ in checksums)
    rows = connection.execute(
        f"""
        with attempts as (
            select content_checksum, batch_id,
                   row_number() over (
                       partition by content_checksum order by seen_at desc
                   ) as recency
              from ops.file_sighting
             where source_system = ? and outcome in ('{NEW}', '{REATTEMPTED}')
               and content_checksum in ({placeholders})
        )
        select a.content_checksum, b.batch_id, b.failure_reason
          from attempts a
          join ops.batch_registry b on b.batch_id = a.batch_id
         where a.recency = 1 and b.status = 'failed'
        """,  # noqa: S608 - placeholders and module constants
        [source_system, *checksums],
    ).fetchall()
    out: dict[str, Parked] = {}
    for checksum_, batch_id, reason in rows:
        if not is_verdict(reason):
            continue
        out[checksum_] = Parked(
            batch_id=batch_id,
            reason=reason,
            fingerprints=attempt_fingerprints(connection, source_system, batch_id),
        )
    return out


def sibling_suffix(batch_id: str) -> str:
    """What the batches of one delivery share: the interval and sequence after the entity."""
    return batch_id.split("-", 1)[1]


def attempt_fingerprints(
    connection: Any, source_system: str, batch_id: str
) -> tuple[tuple[str, str], ...]:
    """The (entity, contract fingerprint) pairs one attempt at a delivery was read against."""
    rows = connection.execute(
        """
        select b.entity, v.contract_fingerprint
          from ops.batch_registry b
          join meta.contract_version v
            on v.source_system = b.source_system and v.entity = b.entity
           and v.contract_version = b.contract_version
         where b.source_system = ? and b.batch_id like ?
        """,
        [source_system, "%-" + sibling_suffix(batch_id)],
    ).fetchall()
    return tuple(sorted((entity, fingerprint) for entity, fingerprint in rows))


def declared_conflict(
    connection: Any,
    *,
    source_system: str,
    checksum_: str,
    business_date: dt.date | None = None,
    file_sequence: int | None = None,
    revision: int | None = None,
    publisher_version: str | None = None,
) -> str | None:
    """The batch that already landed the same declaration with different content, if any.

    The sender's declared fields are attributes, never identity (ADR 0013), and they are still
    a promise: a clearing file's settlement date, sequence and revision, or a snapshot's
    version string, name one delivery. The same declaration over different bytes means the
    sender reused a name it had already given to something else, and landing both would put
    two contradicting deliveries under one name. It fails loudly instead.
    """
    if publisher_version is not None:
        row = connection.execute(
            "select batch_id from ops.ingested_file where source_system = ? "
            "and publisher_version = ? and content_checksum <> ? limit 1",
            [source_system, publisher_version, checksum_],
        ).fetchone()
        return row[0] if row else None
    if business_date is None or file_sequence is None or revision is None:
        return None
    row = connection.execute(
        "select batch_id from ops.ingested_file where source_system = ? and business_date = ? "
        "and file_sequence = ? and revision = ? and content_checksum <> ? limit 1",
        [source_system, business_date, file_sequence, revision, checksum_],
    ).fetchone()
    return row[0] if row else None


def record_sighting(
    connection: Any,
    candidate: Candidate,
    *,
    source_system: str,
    ingest_date: dt.date,
    outcome: str,
    batch_id: str | None,
    run_id: str,
    now: dt.datetime,
) -> None:
    connection.execute(
        """
        insert into ops.file_sighting (
            content_checksum, source_system, object_key, ingest_date, outcome, batch_id,
            triggering_run_id, seen_at
        ) values (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            candidate.checksum,
            source_system,
            candidate.key,
            ingest_date,
            outcome,
            batch_id,
            run_id,
            now,
        ],
    )


def record_ingested(
    connection: Any,
    *,
    checksum_: str,
    source_system: str,
    entity: str,
    key: str,
    size: int,
    business_date: dt.date | None,
    publisher_version: str | None,
    batch_id: str,
    now: dt.datetime,
    file_sequence: int | None = None,
    revision: int | None = None,
) -> bool:
    """Record a file as landed. False if its checksum had already landed, which is refused
    upstream and would mean two batches raced for one file."""
    exists = connection.execute(
        "select batch_id from ops.ingested_file where content_checksum = ?", [checksum_]
    ).fetchone()
    if exists is not None:
        return False
    connection.execute(
        """
        insert into ops.ingested_file (
            content_checksum, source_system, entity, object_key, byte_size, business_date,
            file_sequence, revision, publisher_version, batch_id, first_ingested_at
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            checksum_,
            source_system,
            entity,
            key,
            size,
            business_date,
            file_sequence,
            revision,
            publisher_version,
            batch_id,
            now,
        ],
    )
    return True
