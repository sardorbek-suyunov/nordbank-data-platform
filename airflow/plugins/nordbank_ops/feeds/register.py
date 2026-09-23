"""Register a feed run's batches in one warehouse transaction (spec 006 section 7).

The same step specification 005 built, for sources with no database behind them. A batch whose
report says `failed` is marked failed with its reason and its watermark stays where it was; the
rest register, which is entity-grain "no partial load" exactly as before. In the same
transaction, for what registered:

- the interval feeds' watermark advances to the last date or period requested;
- a delivered file or snapshot is recorded in `ops.ingested_file` by its checksum, so it can
  never land again;
- the vault gains the identifiers the delivery carried, read back from the delivery itself —
  the inbound object, whose checksum is verified first so the re-read cannot see a different
  file from the one that landed. Cleartext travels inbound bucket to register process to vault,
  and reaches nothing else, which is the bound specification 005 set for the relational source.

For every batch, registered or failed: each request's attempts and outcome go to
`ops.feed_request`, drift observations to `meta.schema_drift_log`, and quarantine objects to the
`dq.quarantine_log` index. A failed batch's quarantine is indexed too, because for a breaking
change it is the record of what was refused.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from nordbank_ops import registry
from nordbank_ops.feeds import identity
from nordbank_ops.register import load_quarantine, record_drift, upsert_vault


@dataclass
class FeedRegisterReport:
    registered: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    vault_rows_added: int = 0
    quarantine_rows_loaded: int = 0
    requests_recorded: int = 0
    files_recorded: int = 0

    def as_dict(self) -> dict:
        return {
            "registered": self.registered,
            "failed": [{"entity": e, "reason": r} for e, r in self.failed],
            "vault_rows_added": self.vault_rows_added,
            "quarantine_rows_loaded": self.quarantine_rows_loaded,
            "requests_recorded": self.requests_recorded,
            "files_recorded": self.files_recorded,
        }


def record_requests(connection: Any, batch: dict, requests: list[dict], now: dt.datetime) -> int:
    for entry in requests:
        connection.execute(
            """
            insert into ops.feed_request (
                batch_id, source_system, entity, request_key, attempts, final_status, outcome,
                detail, rows_landed, requested_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict (batch_id, request_key) do update set
                attempts = excluded.attempts, final_status = excluded.final_status,
                outcome = excluded.outcome, detail = excluded.detail,
                rows_landed = excluded.rows_landed, requested_at = excluded.requested_at
            """,
            [
                batch["batch_id"],
                batch["source_system"],
                batch["entity"],
                entry["request_key"],
                entry["attempts"],
                entry["final_status"],
                entry["outcome"],
                (entry.get("detail") or "")[:2000] or None,
                entry["rows_landed"],
                now,
            ],
        )
    return len(requests)


def _timestamp(value) -> dt.datetime | None:
    if value is None or isinstance(value, dt.datetime):
        return value
    return dt.datetime.fromisoformat(value)


def register_feed_run(
    *,
    connection: Any,
    client: Any,
    lake_bucket: str,
    reports: list[dict],
    tokeniser,
    identifier_values,
    now: dt.datetime,
) -> FeedRegisterReport:
    """One transaction over every batch of one feed run.

    `identifier_values(report)` returns, for a registered batch, the `(column, values)` pairs
    to add to the vault, read from the delivery itself; the feed that knows the format supplies
    it, and it returns nothing for a feed that carries no identifier.
    """
    out = FeedRegisterReport()
    connection.execute("begin transaction")
    try:
        for entry in reports:
            batch = registry.batch(connection, entry["batch_id"])
            if batch is None:
                raise registry.RegistryError(
                    f"batch {entry['batch_id']} vanished from the registry"
                )
            entity = batch["entity"]

            record_drift(connection, batch, entry.get("drift", []), now)
            out.requests_recorded += record_requests(
                connection, batch, entry.get("requests", []), now
            )
            out.quarantine_rows_loaded += load_quarantine(
                connection, client, lake_bucket, entry.get("quarantine_keys", [])
            )

            if entry["status"] == "failed":
                registry.mark_failed(connection, batch["batch_id"], entry["failure_reason"], now)
                out.failed.append((entity, entry["failure_reason"]))
                continue

            watermark_to = _timestamp(entry.get("watermark_to"))
            registry.mark_written(
                connection,
                batch["batch_id"],
                rows_read=entry["rows_read"],
                rows_landed=entry["rows_landed"],
                rows_quarantined=entry["rows_quarantined"],
                watermark_to=watermark_to,
                written_at=now,
            )
            registry.mark_registered(connection, batch["batch_id"], now)
            if watermark_to is not None:
                registry.advance_watermark(
                    connection, batch["source_system"], entity, watermark_to, batch["batch_id"], now
                )
            out.registered.append(entity)

            delivery = entry.get("delivery")
            if delivery and delivery.get("records_identity"):
                recorded = identity.record_ingested(
                    connection,
                    checksum_=delivery["checksum"],
                    source_system=batch["source_system"],
                    entity=entity,
                    key=delivery["key"],
                    size=delivery["size"],
                    business_date=(
                        dt.date.fromisoformat(delivery["business_date"])
                        if delivery.get("business_date")
                        else None
                    ),
                    publisher_version=delivery.get("publisher_version"),
                    batch_id=batch["batch_id"],
                    now=now,
                )
                out.files_recorded += int(recorded)

            for column, values in identifier_values(entry):
                out.vault_rows_added += upsert_vault(
                    connection,
                    tokeniser,
                    values,
                    source_system=batch["source_system"],
                    entity=entity,
                    column=column,
                    batch_id=batch["batch_id"],
                    now=now,
                )
        connection.execute("commit")
    except Exception:
        connection.execute("rollback")
        raise
    return out
