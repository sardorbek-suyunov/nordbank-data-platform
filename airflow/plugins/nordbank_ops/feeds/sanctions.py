"""The sanctions list snapshot (spec 006 sections 1 and 2, ADR 0015).

A snapshot is read whole: the publisher's `latest/index.json` names the version, and that
version's `entities.ftm.json` is one FollowTheMoney entity per line. The snapshot is identified
by the checksum of the entities file, so a new version string over unchanged content is
recognised as already landed, and re-landing the same content is a no-op (ADR 0013).

Each line lands as one row with the version and the export timestamp as attributes, and its
payload is the line as delivered: the list carries no column classified identifier, because a
listed name is matched in the clear (ADR 0010) and is classified `sensitive`.

The export timestamp in the index carries no offset, and the publisher's documentation states
it in UTC; the contract says so and the timestamp is read as UTC on that authority.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass

from nordbank_ops.feeds.cast import CastError, cast, utc_without_offset
from nordbank_ops.feeds.land import Parsed
from nordbank_ops.validation import QuarantineReason, Rejection

ENTITIES = "entities.ftm.json"


@dataclass
class Index:
    version: str
    published_at: dt.datetime
    entities_key: str


class SnapshotIndexError(ValueError):
    """The index document does not describe a publication."""


def read_index(body: bytes, prefix: str, contract) -> Index:
    try:
        index = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotIndexError(f"the index is not JSON: {exc}") from exc
    missing = [key for key in contract.format["index_required"] if key not in index]
    if missing:
        raise SnapshotIndexError(f"the index lacks {missing}")
    try:
        published = cast(utc_without_offset(index["last_export"]), "timestamp with time zone")
    except CastError as exc:
        raise SnapshotIndexError(f"the index's last_export does not parse: {exc}") from exc
    return Index(
        version=index["version"],
        published_at=published,
        entities_key=f"{prefix}{index['version']}/{ENTITIES}",
    )


def _path(entity: dict, path: str):
    value = entity
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def read_entities(body: bytes, index: Index, contract) -> Parsed:
    out = Parsed()
    expected = contract.format["top_level"]
    sources = contract.format["column_sources"]
    seen_extra: set[str] = set()
    missing_keys: set[str] = set()
    header = {"publisher_version": index.version, "published_at": index.published_at}

    for line in body.decode("utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entity = json.loads(line)
        except json.JSONDecodeError:
            rejection = Rejection("*", "record is not a JSON object", None, None)
            out.refused.append(({}, rejection, line))
            continue
        if not isinstance(entity, dict):
            rejection = Rejection("*", "record is not a JSON object", None, None)
            out.refused.append(({}, rejection, line))
            continue
        seen_extra.update(key for key in entity if key not in expected)
        missing_keys.update(key for key in expected if key not in entity)

        record = dict(header)
        refused = None
        for column in contract.columns:
            if column.origin != "record":
                continue
            value = _path(entity, sources[column.name])
            if column.data_type.startswith("timestamp") and isinstance(value, str):
                try:
                    value = cast(utc_without_offset(value), column.data_type)
                except CastError:
                    refused = Rejection(
                        column.name, QuarantineReason.TYPE_MISMATCH, value, entity.get("id")
                    )
                    break
            record[column.name] = value
        if refused is not None:
            out.refused.append((record, refused, line))
            continue
        out.records.append(record)
        out.payloads.append(line)

    for key in sorted(seen_extra):
        out.drift.append(
            {
                "column": key,
                "kind": "additive",
                "detail": "a top-level key the contract does not describe",
                "action": "logged, not landed",
            }
        )
    for key in sorted(missing_keys):
        out.drift.append(
            {
                "column": key,
                "kind": "removed_column",
                "detail": "a documented top-level key is absent",
                "action": "snapshot quarantined, batch failed",
            }
        )
    if missing_keys:
        out.breaking = f"documented key(s) absent: {sorted(missing_keys)}"
    return out
