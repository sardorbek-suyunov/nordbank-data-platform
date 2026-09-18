"""Lake key construction and the bucket probe.

The batch id is part of every bronze key, which is what makes an overwrite impossible
(ADR 0008). Key construction lives here so that one function defines the layout for every
producer and every reader.
"""

from __future__ import annotations

from datetime import date
from typing import Any

BRONZE_PREFIX = "bronze"
QUARANTINE_PREFIX = "quarantine"


def bronze_key(
    source: str,
    entity: str,
    ingest_date: date | str,
    batch_id: str,
    part: int = 0,
) -> str:
    day = ingest_date.isoformat() if isinstance(ingest_date, date) else ingest_date
    return (
        f"{BRONZE_PREFIX}/{source}/{entity}/ingest_date={day}/batch_id={batch_id}/"
        f"part-{part:04d}.parquet"
    )


def list_keys(client: Any, bucket: str, prefix: str) -> list[str]:
    response = client.list_objects_v2(Bucket=bucket, Prefix=prefix)
    return [item["Key"] for item in response.get("Contents", [])]


def probe_bucket(client: Any, bucket: str, batch_ids: tuple[str, str], ingest_day: str) -> dict:
    """Write one probe object per batch id under the same ingest date, then remove them.

    Proves the property the bronze layout depends on: two writes that differ only by batch id
    address two keys, so the second cannot overwrite the first.
    """
    if batch_ids[0] == batch_ids[1]:
        raise ValueError("the probe needs two different batch ids to prove anything")

    keys = [
        bronze_key("ops_probe", "stack_healthcheck", ingest_day, batch_id) for batch_id in batch_ids
    ]

    for key in keys:
        client.put_object(Bucket=bucket, Key=key, Body=b"")

    prefix = f"{BRONZE_PREFIX}/ops_probe/stack_healthcheck/ingest_date={ingest_day}/"
    found = sorted(list_keys(client, bucket, prefix))

    try:
        if len(set(keys)) != 2:
            raise AssertionError("key construction collapsed two batch ids into one key")
        missing = [key for key in keys if key not in found]
        if missing:
            raise AssertionError(f"probe objects missing after write: {missing}")
    finally:
        for key in keys:
            client.delete_object(Bucket=bucket, Key=key)

    return {"keys": keys, "found": found}


def assert_prefixes(client: Any, bucket: str) -> list[str]:
    """Both top-level prefixes exist. Returns the prefixes found."""
    present = []
    for prefix in (BRONZE_PREFIX, QUARANTINE_PREFIX):
        if list_keys(client, bucket, f"{prefix}/"):
            present.append(prefix)
    missing = [p for p in (BRONZE_PREFIX, QUARANTINE_PREFIX) if p not in present]
    if missing:
        raise AssertionError(f"bucket {bucket} is missing prefixes: {missing}")
    return present
