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
            publisher_version, batch_id, first_ingested_at
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            checksum_,
            source_system,
            entity,
            key,
            size,
            business_date,
            publisher_version,
            batch_id,
            now,
        ],
    )
    return True
