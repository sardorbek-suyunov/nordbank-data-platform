"""Scan every bronze object for a cleartext identifier (spec 005 criterion 8).

**The values come from `meta.pii_vault`.** That is their designated home, so reading them
reintroduces nothing, and it makes the claim stronger than a hand-picked sample would: not
"these few identifiers are absent" but "no value that was tokenised appears in cleartext
anywhere in bronze".

**Two readings of every object, because either alone misses something.**

- *The object's bytes.* Parquet writes column statistics into its footer uncompressed, and a
  value can survive there after the column itself has been replaced. Decoding the file into
  columns would not see it.
- *Every value decoded.* The data and dictionary pages are snappy-compressed, and a compressed
  page does not hold its strings contiguously: snappy replaces a run it has already seen with a
  back-reference, so a value that shares a prefix with its neighbour is split. Measured at
  specification 006's review: over 400 card-reference-shaped values written with the platform's
  own writer, a byte-wise search found none of them in the snappy object and all 400 in the
  uncompressed one. The byte-wise scan that specifications 005 and 006 first reported was
  therefore blind to column values; it could see only statistics and literal runs.

**Every encoding the objects use.** A value is searched as itself; with any fixed-width padding
removed, because a `character(n)` value may be stored padded in one place and trimmed in
another; as JSON escapes it, both ASCII-escaped and not, because an API payload is a JSON
document; and with its double quotes doubled, as a CSV payload quotes it. A value is not
searched with separators around it, because a separator can only make a match stricter.

**A name on the sanctions list is the list's, not the bank's.** A vault value found in a
sanctions snapshot is excused only when the publisher's delivered file carries it too, which is
what a screening match looks like and what the M3 fixture plants on purpose; it is counted and
reported, never silently dropped. The excuse is scoped twice: only for an object under the
sanctions source's own bronze or quarantine prefix, and only for a value present in the file
delivered for that object's snapshot. The same name anywhere else is a hit.

**No value is ever printed.** A hit reports the token, the object, which reading found it and
the length of the value that matched; the value itself stays in the vault. A report that named
the cleartext it found would be the leak it was written to detect.

Two limits are stated rather than left for a reader to assume. A short value can appear inside
an unrelated byte sequence by chance, so a hit is a candidate rather than a verdict and its
length is printed for judgement. And a column that is classified `identifier` but is null in
every row the source produces contributes nothing to the vault, so the scan proves nothing
about it; those columns are listed at the end so the coverage claim is honest.

`--plant` proves the scan can fail, three ways. It copies one registered clearing-file object,
replaces one card token in it with the card's cleartext reference, writes the copy with the
platform's own writer to the scratch bucket, and scans the copy both ways; the byte-wise reading
alone is reported beside the full one, so the evidence shows what the first version of this scan
would have concluded. Then it proves the sanctions excuse is scoped: a fixture name the list
carries, planted in a copy of a core banking payments object, is caught; a card reference the
list does not carry, planted in a copy of a sanctions object, is caught; and the unplanted
sanctions object is excused, which is the case the excuse exists for. Each copy is judged by the
function the scan itself uses, under the key of the object it copies.

Runs inside a container: the warehouse is on a named volume.
"""

from __future__ import annotations

import io
import json
import os
import sys

sys.path.insert(0, "/opt/airflow/plugins")

# A value shorter than this is too likely to occur by chance inside unrelated bytes for a hit
# to mean anything. Reported separately rather than dropped.
SHORT_VALUE = 6

SCRATCH_BUCKET = "nordbank-fault-demo"
PLANT_PREFIX = "pii-scan-plant/"

# Where a value the sanctions list carries may be excused: the sanctions source's own objects.
SANCTIONS_PREFIXES = ("bronze/opensanctions/", "quarantine/opensanctions/")


def encodings(value: str) -> set[bytes]:
    """Every byte form a value can take in a bronze or quarantine object."""
    forms = {value, value.strip()}
    for form in list(forms):
        forms.add(json.dumps(form)[1:-1])
        forms.add(json.dumps(form, ensure_ascii=False)[1:-1])
        forms.add(form.replace('"', '""'))
    return {form.encode("utf-8") for form in forms if len(form) >= SHORT_VALUE}


def decoded_text(body: bytes) -> bytes:
    """Every value of every column of a Parquet object, decompressed, one per line."""
    import pyarrow.parquet as pq

    table = pq.read_table(io.BytesIO(body), use_threads=False)
    parts: list[str] = []
    for column in table.columns:
        for value in column.to_pylist():
            if value is not None:
                parts.append(value if isinstance(value, str) else str(value))
    return "\n".join(parts).encode("utf-8")


class Matcher:
    """Every encoded form of every vault value, indexed by its first `SHORT_VALUE` bytes.

    Searching a hundred thousand forms one at a time in each of three thousand objects took
    nearly nine minutes. Indexing the forms by prefix makes a reading one pass over its bytes:
    each position is looked up once, and only a prefix that matches is compared in full.
    """

    def __init__(self, needles: list[tuple[str, set[bytes]]]) -> None:
        self.by_prefix: dict[bytes, list[tuple[bytes, str]]] = {}
        for token, forms in needles:
            for form in forms:
                self.by_prefix.setdefault(form[:SHORT_VALUE], []).append((form, token))

    def tokens_in(self, haystack: bytes) -> dict[str, int]:
        found: dict[str, int] = {}
        index = self.by_prefix
        for position in range(len(haystack) - SHORT_VALUE + 1):
            candidates = index.get(haystack[position : position + SHORT_VALUE])
            if candidates:
                for form, token in candidates:
                    if haystack.startswith(form, position):
                        found.setdefault(token, len(form))
        return found


def find(body: bytes, needles) -> list[tuple[str, str, int]]:
    """(token, reading, length) for every vault value present in an object."""
    matcher = needles if isinstance(needles, Matcher) else Matcher(needles)
    hits = []
    for reading, haystack in (("bytes", body), ("decoded", decoded_text(body))):
        for token, length in matcher.tokens_in(haystack).items():
            hits.append((token, reading, length))
    return hits


def vault_values(connection) -> list[tuple[str, str]]:
    return connection.execute("select token, raw_value from meta.pii_vault").fetchall()


def classified_identifier_columns(cursor) -> list[tuple[str, str, str]]:
    cursor.execute(
        """
        select schema_name, table_name, column_name
          from platform.column_classifications
         where classification = 'identifier'
         order by 1, 2, 3
        """
    )
    return list(cursor.fetchall())


def registered_objects(connection) -> list[str]:
    """Every bronze prefix of a registered batch, and every quarantine prefix of any batch.

    Quarantine is scanned for a failed batch too: a file refused whole for breaking drift is
    quarantined record by record with its payload, and a cleartext identifier there would be as
    much a leak as one in bronze (specification 006 criterion 12).
    """
    rows = connection.execute(
        "select object_prefix, status from ops.batch_registry "
        "where status in ('registered', 'failed')"
    ).fetchall()
    prefixes = [prefix for prefix, status in rows if status == "registered"]
    prefixes += ["quarantine/" + prefix.split("/", 1)[1] for prefix, _status in rows]
    return prefixes


def _keys(client, bucket: str, prefixes) -> list[str]:
    keys: list[str] = []
    for prefix in sorted(set(prefixes)):
        token = None
        while True:
            arguments = {"Bucket": bucket, "Prefix": prefix}
            if token:
                arguments["ContinuationToken"] = token
            response = client.list_objects_v2(**arguments)
            keys.extend(item["Key"] for item in response.get("Contents", []))
            token = response.get("NextContinuationToken")
            if not token:
                break
    return keys


def plant(client, bucket: str, connection) -> int:
    """Plant one cleartext card reference in a copy of a real object and scan the copy."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    (prefix,) = connection.execute(
        "select object_prefix from ops.batch_registry where entity = 'settlements' "
        "and status = 'registered' and rows_landed > 100 order by batch_id limit 1"
    ).fetchone()
    (key,) = _keys(client, bucket, [prefix])
    table = pq.read_table(io.BytesIO(client.get_object(Bucket=bucket, Key=key)["Body"].read()))
    tokens = table.column("card_reference").to_pylist()
    victim = tokens[len(tokens) // 2]
    (raw,) = connection.execute(
        "select raw_value from meta.pii_vault where token = ?", [victim]
    ).fetchone()
    planted = [raw if i == len(tokens) // 2 else t for i, t in enumerate(tokens)]
    index = table.schema.get_field_index("card_reference")
    table = table.set_column(index, "card_reference", pa.array(planted, pa.string()))

    buffer = io.BytesIO()
    pq.write_table(table, buffer, compression="snappy")
    body = buffer.getvalue()
    buckets = {b["Name"] for b in client.list_buckets()["Buckets"]}
    if SCRATCH_BUCKET not in buckets:
        client.create_bucket(Bucket=SCRATCH_BUCKET)
    target = PLANT_PREFIX + key
    client.put_object(Bucket=SCRATCH_BUCKET, Key=target, Body=body)

    hits = find(body, [(victim, encodings(raw))])
    readings = sorted({reading for _token, reading, _length in hits})
    print(f"bronze-pii-scan --plant: copied {key}")
    print(
        f"bronze-pii-scan --plant: replaced token {victim} with its cleartext "
        f"({len(raw)} bytes) and wrote {SCRATCH_BUCKET}/{target} with the platform's writer"
    )
    print(f"bronze-pii-scan --plant: byte-wise reading alone finds it: {'bytes' in readings}")
    print(f"bronze-pii-scan --plant: decoded reading finds it: {'decoded' in readings}")
    client.delete_object(Bucket=SCRATCH_BUCKET, Key=target)
    print(f"bronze-pii-scan --plant: deleted {SCRATCH_BUCKET}/{target}")
    if "decoded" not in readings:
        print("bronze-pii-scan --plant: the scan did NOT catch a planted identifier")
        return 1
    print("bronze-pii-scan --plant: caught")
    return 0


def in_sanctions_scope(key: str) -> bool:
    return key.startswith(SANCTIONS_PREFIXES)


def judge(client, inbound: str, key: str, body: bytes, matcher, deliveries, carried) -> list:
    """The hits in one object: every vault value found, less those the sanctions list excuses.

    The one decision the scan makes about an object, used for the lake's own objects and for
    the planted copies alike. `key` decides the scope, so a copy is judged as the object it
    copies.
    """
    found = find(body, matcher)
    if found and in_sanctions_scope(key):
        found = _not_carried_by_the_list(client, inbound, key, deliveries, matcher, found, carried)
    return found


def _registered_object(client, bucket: str, connection, system: str, entity: str) -> str:
    (prefix,) = connection.execute(
        "select object_prefix from ops.batch_registry where source_system = ? and entity = ? "
        "and status = 'registered' and rows_landed > 0 order by batch_id desc limit 1",
        [system, entity],
    ).fetchone()
    (key,) = _keys(client, bucket, [prefix])
    return key


def _replace_one(body: bytes, column: str, value: str) -> bytes:
    """The object with its first non-null `column` value replaced, written the platform's way."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pq.read_table(io.BytesIO(body))
    values = table.column(column).to_pylist()
    at = next(i for i, v in enumerate(values) if v is not None)
    values[at] = value
    field = table.schema.field(column)
    table = table.set_column(
        table.schema.get_field_index(column), field, pa.array(values, field.type)
    )
    buffer = io.BytesIO()
    pq.write_table(table, buffer, compression="snappy")
    return buffer.getvalue()


def plant_outside_the_list(client, bucket: str, connection) -> int:
    """Prove the sanctions excuse holds only where it should (specification 006 review, C6)."""
    inbound = os.environ["INBOUND_BUCKET"]
    deliveries = dict(
        connection.execute(
            "select batch_id, object_key from ops.ingested_file "
            "where source_system = 'opensanctions'"
        ).fetchall()
    )
    pairs = vault_values(connection)
    matcher = Matcher([(t, encodings(v)) for t, v in pairs if len(v.strip()) >= SHORT_VALUE])
    raw_of = dict(pairs)

    snapshot = _registered_object(client, bucket, connection, "opensanctions", "entities")
    snapshot_body = client.get_object(Bucket=bucket, Key=snapshot)["Body"].read()
    carried: set[str] = set()
    left = judge(client, inbound, snapshot, snapshot_body, matcher, deliveries, carried)
    print(
        f"bronze-pii-scan --plant: {snapshot} as landed: {len(carried)} list value(s) excused, "
        f"{len(left)} hit(s)"
    )
    if not carried or left:
        print("bronze-pii-scan --plant: the landed snapshot is not the case the excuse is for")
        return 1
    fixture = sorted(carried)[0]

    payments = _registered_object(client, bucket, connection, "corebank", "payments")
    planted = _replace_one(
        client.get_object(Bucket=bucket, Key=payments)["Body"].read(),
        "counterparty_name",
        raw_of[fixture],
    )
    found = judge(client, inbound, payments, planted, matcher, deliveries, set())
    in_core = fixture in {token for token, _reading, _length in found}
    print(
        f"bronze-pii-scan --plant: fixture name {fixture} ({len(raw_of[fixture])} bytes), which "
        f"the list carries, planted in a copy of {payments}: caught {in_core}"
    )

    (card,) = connection.execute(
        "select token from meta.pii_vault where first_seen_column = 'card_reference' "
        "order by token limit 1"
    ).fetchone()
    planted = _replace_one(snapshot_body, "caption", raw_of[card])
    found = judge(client, inbound, snapshot, planted, matcher, deliveries, set())
    in_list = card in {token for token, _reading, _length in found}
    print(
        f"bronze-pii-scan --plant: card reference {card}, which the snapshot's delivered file "
        f"does not carry, planted in a copy of {snapshot}: caught {in_list}"
    )
    if not (in_core and in_list):
        print("bronze-pii-scan --plant: the sanctions excuse reached a value it must not")
        return 1
    print("bronze-pii-scan --plant: the sanctions excuse holds only for the list's own content")
    return 0


def _not_carried_by_the_list(client, inbound, key, deliveries, matcher, found, carried) -> list:
    """The hits in a sanctions object that the simulated publisher's file does not carry.

    A sanctions list is a list of names, and a name on it can equal a name the bank vaulted:
    that is a screening match, and the M3 fixture plants exactly such names so screening has
    something to find. Such a value in the landed snapshot is the list's content, not a leak
    of the bank's data. It is excused only on proof: the same value must be in the file the
    publisher delivered, read from the inbound bucket. Anything else is a hit.
    """
    batch_id = key.split("batch_id=", 1)[1].split("/", 1)[0]
    source = deliveries.get(batch_id)
    if source is None:
        return found
    delivered = client.get_object(Bucket=inbound, Key=source)["Body"].read()
    in_delivery = matcher.tokens_in(delivered)
    remaining = []
    for token, reading, length in found:
        if token in in_delivery:
            carried.add(token)
        else:
            remaining.append((token, reading, length))
    return remaining


def main(argv: list[str]) -> int:
    from nordbank_ops import clients, warehouse

    client = clients.lake_client()
    bucket = clients.lake_bucket()

    with warehouse.connect(read_only=True) as connection:
        if "--plant" in argv:
            caught = plant(client, bucket, connection)
            scoped = plant_outside_the_list(client, bucket, connection)
            return caught or scoped
        pairs = vault_values(connection)
        prefixes = registered_objects(connection)
        # Where each vault value was first seen. A classified identifier column that appears
        # nowhere in this set supplied nothing, which is what a column that is null in every
        # row looks like, and the scan cannot speak for it.
        sighted = {
            (entity, column)
            for entity, column in connection.execute(
                "select distinct first_seen_entity, first_seen_column from meta.pii_vault"
            ).fetchall()
        }
        deliveries = dict(
            connection.execute(
                "select batch_id, object_key from ops.ingested_file "
                "where source_system = 'opensanctions'"
            ).fetchall()
        )

    with clients.source_cursor() as cursor:
        classified = classified_identifier_columns(cursor)

    short = [(t, v) for t, v in pairs if len(v.strip()) < SHORT_VALUE]
    needles = [(t, encodings(v)) for t, v in pairs if len(v.strip()) >= SHORT_VALUE]
    keys = _keys(client, bucket, prefixes)

    print(
        f"bronze-pii-scan: {len(keys)} object(s), "
        f"{sum(1 for k in keys if k.startswith('quarantine/'))} of them quarantine, under "
        f"{len(set(prefixes))} prefix(es), against {len(pairs)} vault value(s) in "
        f"{sum(len(forms) for _t, forms in needles)} encoded form(s)"
    )
    # A scan of nothing, or for nothing, finds nothing, and would report that as a pass.
    if not keys or not needles:
        print(
            "bronze-pii-scan: refusing to report a result: "
            f"{len(keys)} object(s) to scan and {len(needles)} vault value(s) to scan for. "
            "Both must be non-zero for the absence of a hit to mean anything."
        )
        return 2

    matcher = Matcher(needles)
    hits: list[tuple[str, str, str, int]] = []
    carried: set[str] = set()
    scanned = decoded = 0
    inbound = os.environ["INBOUND_BUCKET"]
    for key in sorted(keys):
        body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        scanned += len(body)
        decoded += len(decoded_text(body))
        found = judge(client, inbound, key, body, matcher, deliveries, carried)
        for token, reading, length in found:
            hits.append((token, key, reading, length))

    print(
        f"bronze-pii-scan: read {scanned / 1_000_000:.1f} MB, decoded {decoded / 1_000_000:.1f} MB"
    )
    print(f"bronze-pii-scan: {len(short)} vault value(s) shorter than {SHORT_VALUE} bytes, skipped")

    unproven = sorted(
        f"{schema}.{table}.{column}"
        for schema, table, column in classified
        if (table, column) not in sighted
    )

    print(
        f"bronze-pii-scan: {len(carried)} vault value(s) found in sanctions snapshots and "
        "present in the publisher's delivered file itself: list content, not a leak"
    )
    if hits:
        print(f"\nbronze-pii-scan: {len(hits)} candidate hit(s):")
        for token, key, reading, length in hits[:50]:
            print(f"  token {token} ({length} bytes, {reading}) in {key}")
        return 1

    print(
        "\nbronze-pii-scan: no cleartext identifier found in any registered bronze object or "
        "any quarantine object, in either reading"
    )
    if unproven:
        print(
            "bronze-pii-scan: these classified identifier columns supplied no value to the "
            "vault, so the scan proves nothing about them:"
        )
        for column in unproven:
            print(f"  {column}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
