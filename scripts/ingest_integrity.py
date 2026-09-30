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

5. **Does every terminal batch's quarantined count equal its rows in the quarantine index?**
   One fact in two places: the registry's `rows_quarantined`, written by the register step from
   the extract step's report, and `dq.quarantine_log`, indexed by the same step from the
   quarantine objects. Two representations of one fact need a check, and this is it, for
   failed batches as much as registered ones.
6. **Does every identifier token in registered bronze resolve in the vault?** The extract task
   tokenises and discards the cleartext; the register step re-reads the raw values from the
   source or the delivery and vaults them. A value that changed between the two reads would
   leave a token in bronze that nothing resolves: a subject who could then be neither erased
   nor screened, because both go through the vault (ADR 0005). The backfill cannot produce it,
   since nothing writes to the source between a day's extract and its register step; a deployed
   source that is written to all day can. Every identifier column of every contract version is
   read from every registered object and each distinct token looked up.
7. **Does any entity carry more physical schemas than it has contract versions?** The writer
   writes each batch in the schema of the contract version it was validated against
   (`nordbank_ops.physical`), so an entity can have at most one physical schema per version.
   More means a writer that infers types from values again, which is what produced between two
   and seven schemas for 13 entities in the fourth acceptance run, and what a reader that does
   not combine files by name reads wrongly. A schema is the ordered column names and their
   Arrow types.

Checks 6 and 7 read every registered object once, in one pass.

Exit code 0 when 1, 2, 4, 5, 6 and 7 are clean, whatever 3 says. Runs inside a container.
"""

from __future__ import annotations

import io
import sys
from collections.abc import Iterable, Mapping

for _path in ("/opt/airflow/plugins", "/opt/airflow/scripts"):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from nordbank_ops import clients, warehouse  # noqa: E402

Entity = tuple[str, str]


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

        disagreeing = connection.execute(
            """
            select b.batch_id, b.status, b.rows_quarantined, coalesce(q.rows, 0)
              from ops.batch_registry b
              left join (select batch_id, count(*) as rows from dq.quarantine_log
                          group by batch_id) q using (batch_id)
             where b.status in ('registered', 'failed')
               and b.rows_quarantined <> coalesce(q.rows, 0)
             order by b.batch_id
            """
        ).fetchall()
        vault = {
            token for (token,) in connection.execute("select token from meta.pii_vault").fetchall()
        }
        registered = connection.execute(
            """
            select source_system, entity, object_prefix from ops.batch_registry
             where status = 'registered' and rows_landed > 0 order by entity, batch_id
            """
        ).fetchall()
        (quarantined_total,) = connection.execute(
            "select coalesce(sum(rows_quarantined), 0) from ops.batch_registry "
            "where status in ('registered', 'failed')"
        ).fetchone()

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

    print(
        f"\n5. terminal batches whose quarantined count differs from the quarantine index: "
        f"{len(disagreeing)} (registry total {quarantined_total})"
    )
    for batch_id, status, claimed, indexed in disagreeing[:20]:
        print(f"   {batch_id} ({status}): registry {claimed}, quarantine index {indexed}")

    chains = _contract_chains()
    columns = identifier_columns(chains)
    scoped = [
        (system, entity, prefix)
        for system, entity, prefix in registered
        if (system, entity) in columns
    ]
    tokens, schemas = scan_objects(client, bucket, registered, columns)
    unresolved = unresolved_tokens(tokens, vault)
    checked = sum(len(found) for found in tokens.values())
    distinct = len(set().union(*tokens.values())) if tokens else 0
    print(
        f"\n6. identifier tokens in registered bronze with no vault row: "
        f"{sum(len(found) for found in unresolved.values())} of {checked} (column, token) pair(s), "
        f"{distinct} distinct token(s), "
        f"over {len(scoped)} registered batch(es) of {len(columns)} entities with identifier "
        f"columns, against {len(vault)} vault row(s)"
    )
    for (system, entity, column), found in sorted(unresolved.items()):
        print(f"   {system}.{entity}.{column}: {len(found)} unresolved, e.g. {sorted(found)[0]}")
    # A check that read nothing proves nothing: registered batches of entities that carry
    # identifiers exist, so some token must have been read.
    vacuous = bool(scoped) and checked == 0
    if vacuous:
        print("   read no token from batches that should carry identifiers; not a clean result")

    versions = {key: len(chain) for key, chain in chains.items()}
    beyond = schemas_beyond_versions(schemas, versions)
    print(
        f"\n7. entities with more physical schemas than contract versions: {len(beyond)} of "
        f"{len(schemas)} entities with objects, over {sum(len(s) for s in schemas.values())} "
        f"distinct schema(s)"
    )
    for (system, entity), (found, allowed) in sorted(beyond.items()):
        print(f"   {system}.{entity}: {found} physical schema(s), {allowed} contract version(s)")

    # An empty registry has no open batch, no missing object and no watermark ahead of
    # anything, so every question above answers "none". That is not a clean registry.
    if not batches:
        print("\nintegrity: nothing to check, the registry holds no batch")
        return 2

    clean = (
        not still_open
        and not missing
        and not ahead
        and not disagreeing
        and not unresolved
        and not vacuous
        and not beyond
    )
    print(f"\nintegrity: {'clean' if clean else 'NOT CLEAN'} across {len(batches)} batch(es)")
    return 0 if clean else 1


def _contract_chains():
    """Every version of every contract of every source, keyed on (source system, entity)."""
    from data_contract import load_history
    from nordbank_ops.ingest import contract_root

    root = contract_root()
    chains = {}
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        for entity, versions in load_history(directory).items():
            chains[(versions[-1].source_system, entity)] = versions
    return chains


def identifier_columns(chains: Mapping[Entity, Iterable]) -> dict[Entity, tuple[str, ...]]:
    """The identifier columns of each entity, across every contract version it has had.

    A column an older version classified as an identifier is still in the objects that version
    wrote, so a retired column is checked as long as those objects exist.
    """
    out = {}
    for key, versions in chains.items():
        names = sorted(
            {
                c.name
                for version in versions
                for c in version.columns
                if c.classification == "identifier"
            }
        )
        if names:
            out[key] = tuple(names)
    return out


def scan_objects(client, bucket: str, batches, columns):
    """One pass over every object of `batches`: identifier tokens, and physical schemas.

    Returns the distinct non-null value of every identifier column per entity and column, and
    the distinct physical schemas per entity.
    """
    import pyarrow.parquet as pq

    tokens: dict[tuple[str, str, str], set] = {}
    schemas: dict[Entity, set] = {}
    for system, entity, prefix in batches:
        wanted = columns.get((system, entity), ())
        for key in _keys(client, bucket, prefix):
            body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
            handle = pq.ParquetFile(io.BytesIO(body))
            schemas.setdefault((system, entity), set()).add(physical_schema(handle.schema_arrow))
            present = [name for name in wanted if name in handle.schema_arrow.names]
            if not present:
                continue
            table = handle.read(columns=present)
            for name in present:
                values = {v for v in table.column(name).to_pylist() if v is not None}
                tokens.setdefault((system, entity, name), set()).update(values)
    return tokens, schemas


def physical_schema(schema) -> tuple[tuple[str, str], ...]:
    """An object's schema as the ordered column names and their Arrow types, metadata ignored."""
    return tuple((field.name, str(field.type)) for field in schema)


def schemas_beyond_versions(schemas: Mapping, versions: Mapping) -> dict:
    """Entities whose objects carry more distinct schemas than they have contract versions."""
    return {
        entity: (len(found), versions.get(entity, 0))
        for entity, found in schemas.items()
        if len(found) > versions.get(entity, 0)
    }


def unresolved_tokens(tokens: Mapping, vault: set) -> dict:
    """The tokens, per entity and column, that have no vault row."""
    return {where: values - vault for where, values in tokens.items() if values - vault}


def _keys(client, bucket: str, prefix: str) -> list[str]:
    response = client.list_objects_v2(Bucket=bucket, Prefix=prefix)
    return [item["Key"] for item in response.get("Contents", [])]


def _count(client, bucket: str, prefix: str) -> int:
    response = client.list_objects_v2(Bucket=bucket, Prefix=prefix)
    return len(response.get("Contents", []))


if __name__ == "__main__":
    raise SystemExit(main())
