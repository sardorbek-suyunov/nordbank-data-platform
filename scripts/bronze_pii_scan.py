"""Scan every bronze object for a cleartext identifier (spec 005 criterion 8).

**The values come from `meta.pii_vault`.** That is their designated home, so reading them
reintroduces nothing, and it makes the claim stronger than a hand-picked sample would: not
"these few identifiers are absent" but "no value that was tokenised appears in cleartext
anywhere in bronze".

**The scan is byte-wise, not column-wise.** Parquet writes column statistics and dictionary
pages alongside the data, and a value can survive in either after the column itself has been
replaced. A scan that decoded the file into columns and looked at those would not see it.
Reading the object's bytes and searching for the UTF-8 encoding of each value does.

**No value is ever printed.** A hit reports the token, the object and the length of the value
that matched; the value itself stays in the vault. A report that named the cleartext it found
would be the leak it was written to detect.

Two limits are stated rather than left for a reader to assume. A short value can appear inside
an unrelated byte sequence by chance, so a hit is a candidate rather than a verdict and its
length is printed for judgement. And a column that is classified `identifier` but is null in
every row the source produces contributes nothing to the vault, so the scan proves nothing
about it; those columns are listed at the end so the coverage claim is honest.

Runs inside a container: the warehouse is on a named volume.
"""

from __future__ import annotations

import sys

sys.path.insert(0, "/opt/airflow/plugins")

from nordbank_ops import clients, warehouse  # noqa: E402

# A value shorter than this is too likely to occur by chance inside unrelated bytes for a hit
# to mean anything. Reported separately rather than dropped.
SHORT_VALUE = 6


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
    rows = connection.execute(
        "select object_prefix from ops.batch_registry where status = 'registered'"
    ).fetchall()
    return [row[0] for row in rows]


def main() -> int:
    client = clients.lake_client()
    bucket = clients.lake_bucket()

    with warehouse.connect(read_only=True) as connection:
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

    with clients.source_cursor() as cursor:
        classified = classified_identifier_columns(cursor)

    short = [(t, v) for t, v in pairs if len(v) < SHORT_VALUE]
    usable = [(t, v.encode("utf-8")) for t, v in pairs if len(v) >= SHORT_VALUE]

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

    print(
        f"bronze-pii-scan: {len(keys)} object(s) under {len(set(prefixes))} registered "
        f"prefix(es), against {len(pairs)} vault value(s)"
    )

    hits: list[tuple[str, str, int]] = []
    scanned = 0
    for key in sorted(keys):
        body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        scanned += len(body)
        for token, needle in usable:
            if needle in body:
                hits.append((token, key, len(needle)))

    print(f"bronze-pii-scan: scanned {scanned / 1_000_000:.1f} MB")
    print(f"bronze-pii-scan: {len(short)} vault value(s) shorter than {SHORT_VALUE} bytes, skipped")

    unproven = sorted(
        f"{schema}.{table}.{column}"
        for schema, table, column in classified
        if (table, column) not in sighted
    )

    if hits:
        print(f"\nbronze-pii-scan: {len(hits)} candidate hit(s):")
        for token, key, length in hits[:50]:
            print(f"  token {token} ({length} bytes) in {key}")
        return 1

    print("\nbronze-pii-scan: no cleartext identifier found in any registered bronze object")
    if unproven:
        print(
            "bronze-pii-scan: these classified identifier columns supplied no value to the "
            "vault, so the scan proves nothing about them:"
        )
        for column in unproven:
            print(f"  {column}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
