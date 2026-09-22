"""Is the registry consistent with the lake and with the watermarks?

Four questions, and each one is a failure the three-phase split is supposed to make
impossible. Written when an ungraceful kill — a laptop shut down in the middle of a resumed
backfill — made it necessary to establish the state rather than assume it, and kept because a
graceful run should be able to answer the same four questions at any time.

1. **Is any batch still `open`?** `open` means a batch was allocated and nothing ever reported
   on it. The register step fails such a batch explicitly, so one surviving a finished run is
   a batch no register step ever saw — the loop died between open and register.
2. **Does every `registered` batch have its objects?** A registered batch is readable
   downstream by definition, so a registered batch with no object is bronze promising data it
   does not have. This is the one outright defect of the four.
3. **Is there an orphan object under an unregistered batch?** Expected and inert: a run that
   writes and then dies leaves objects that no registered batch covers, every bronze model
   filters on the registry, and the batch id is reused when the batch is retried so the
   objects are overwritten (ADR 0008). Reported so the inertness is confirmed rather than
   assumed. The reaper that removes them is M7's.
4. **Is any watermark ahead of its entity's highest registered batch?** The watermark advances
   only in the register transaction, so a watermark past the last registered batch would mean
   the platform has forgotten to re-read rows nothing has landed. This is the failure the
   whole split exists to prevent.

Exit code 0 when 1, 2 and 4 are clean, whatever 3 says. Runs inside a container.
"""

from __future__ import annotations

import sys

sys.path.insert(0, "/opt/airflow/plugins")

from nordbank_ops import clients, warehouse  # noqa: E402


def main() -> int:
    client = clients.lake_client()
    bucket = clients.lake_bucket()

    with warehouse.connect(read_only=True) as connection:
        by_status = connection.execute(
            "select status, count(*) from ops.batch_registry group by 1 order by 1"
        ).fetchall()
        batches = connection.execute(
            """
            select batch_id, entity, status, object_prefix, rows_landed
              from ops.batch_registry order by entity, batch_id
            """
        ).fetchall()
        watermarks = connection.execute(
            """
            select w.entity, w.watermark_at, w.advanced_by_batch_id,
                   (select max(b.watermark_to) from ops.batch_registry b
                     where b.entity = w.entity and b.status = 'registered')
              from ops.extract_watermark w order by w.entity
            """
        ).fetchall()

    print("batches by status:")
    for status, count in by_status:
        print(f"  {status}: {count}")

    still_open = [row for row in batches if row[2] == "open"]
    print(f"\n1. batches still open: {len(still_open)}")
    for row in still_open[:20]:
        print(f"   {row[0]} ({row[1]})")

    missing = []
    orphans = []
    for batch_id, entity, status, prefix, landed in batches:
        found = _count(client, bucket, prefix)
        if status == "registered" and landed > 0 and found == 0:
            missing.append((batch_id, entity, landed))
        if status in ("open", "written") and found > 0:
            orphans.append((batch_id, entity, status, found))

    print(f"\n2. registered batches whose objects are absent: {len(missing)}")
    for batch_id, entity, landed in missing[:20]:
        print(f"   {batch_id} ({entity}) claims {landed} landed row(s) and has no object")

    print(f"\n3. objects under an unregistered batch: {len(orphans)}")
    for batch_id, entity, status, found in orphans[:20]:
        print(f"   {batch_id} ({entity}) is {status} and has {found} object(s)")
    if orphans:
        print(
            "   inert by design: every bronze model filters on the registry, and the batch id "
            "is reused when the batch is retried, so these are overwritten rather than read"
        )

    ahead = [
        (entity, held, by_batch, registered)
        for entity, held, by_batch, registered in watermarks
        if held is not None and (registered is None or held > registered)
    ]
    print(f"\n4. watermarks ahead of their highest registered batch: {len(ahead)}")
    for entity, held, by_batch, registered in ahead[:20]:
        print(f"   {entity}: watermark {held} from {by_batch}, highest registered {registered}")

    clean = not still_open and not missing and not ahead
    print(f"\nintegrity: {'clean' if clean else 'NOT CLEAN'}")
    return 0 if clean else 1


def _count(client, bucket: str, prefix: str) -> int:
    response = client.list_objects_v2(Bucket=bucket, Prefix=prefix)
    return len(response.get("Contents", []))


if __name__ == "__main__":
    raise SystemExit(main())
