"""Plant objects the registry never registered, and show the bronze model keeps them out.

Specification 007, criterion 4, on a real stack rather than on fixtures. `plant` writes two
objects under a registered entity's prefix in the lake:

- a byte-for-byte copy of a registered object under a new key, whose batch id the registry has
  never seen: its `_batch_id` column still names the registered batch, which is why the filter
  reads the object key and not that column;
- an object under an unregistered batch id whose `_batch_id` agrees with its key, which is what
  a run that died after writing leaves behind.

`verify`, after `dbt build`, reads the bronze view and requires none of the planted rows in it,
while reading the planted objects directly shows they are in the lake. `remove` deletes them.

Runs inside the scheduler container, where the lake connection, the warehouse and the image's
httpfs are. The planted keys are kept in a file there between the steps.
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

for _path in ("/opt/airflow/plugins", "/opt/airflow/scripts"):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from nordbank_ops import clients, warehouse  # noqa: E402

STATE = Path("/tmp/bronze-plant.json")
SOURCE_SYSTEM, ENTITY = "cardnet", "settlements"


def _registered_object(client, bucket: str) -> tuple[str, str, str]:
    """A registered batch of the entity with rows, its ingest date and its object key."""
    with warehouse.connect(read_only=True) as connection:
        row = connection.execute(
            """
            select batch_id, ingest_date, object_prefix from ops.batch_registry
             where status = 'registered' and rows_landed > 0
               and source_system = ? and entity = ?
             order by batch_id limit 1
            """,
            [SOURCE_SYSTEM, ENTITY],
        ).fetchone()
    if row is None:
        raise SystemExit(f"bronze-plant: no registered {SOURCE_SYSTEM}.{ENTITY} batch with rows")
    batch_id, ingest_date, prefix = row
    contents = client.list_objects_v2(Bucket=bucket, Prefix=prefix).get("Contents", [])
    return batch_id, ingest_date.isoformat(), contents[0]["Key"]


def plant() -> int:
    import pyarrow.parquet as pq

    client, bucket = clients.lake_client(), clients.lake_bucket()
    batch_id, ingest_date, key = _registered_object(client, bucket)
    body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
    base = f"bronze/{SOURCE_SYSTEM}/{ENTITY}/ingest_date={ingest_date}"

    copy_id = batch_id.rsplit("-", 1)[0] + "-97"
    copy_key = f"{base}/batch_id={copy_id}/part-0000.parquet"
    client.put_object(Bucket=bucket, Key=copy_key, Body=body)

    orphan_id = batch_id.rsplit("-", 1)[0] + "-98"
    table = pq.read_table(io.BytesIO(body))
    column = table.schema.get_field_index("_batch_id")
    table = table.set_column(column, table.schema.field(column), [[orphan_id] * table.num_rows])
    buffer = io.BytesIO()
    pq.write_table(table, buffer, compression="snappy")
    orphan_key = f"{base}/batch_id={orphan_id}/part-0000.parquet"
    client.put_object(Bucket=bucket, Key=orphan_key, Body=buffer.getvalue())

    planted = {
        "copy": [copy_id, copy_key],
        "orphan": [orphan_id, orphan_key],
        "rows": table.num_rows,
    }
    STATE.write_text(json.dumps(planted), encoding="utf-8")
    print(f"bronze-plant: a copy of {batch_id} under {copy_key}, {table.num_rows} row(s)")
    print(f"bronze-plant: an unregistered batch under {orphan_key}, {table.num_rows} row(s)")
    return 0


def verify() -> int:
    planted = json.loads(STATE.read_text(encoding="utf-8"))
    connection_ = clients._connection(clients.LAKE_CONN_ID)
    endpoint = connection_.extra_dejson["endpoint_url"].split("://", 1)[1]
    bucket = clients.lake_bucket()
    with warehouse.connect(read_only=True) as connection:
        connection.execute(
            f"set extension_directory = '{os.environ['DUCKDB_EXTENSION_DIRECTORY']}'"
        )
        connection.execute("load httpfs")
        connection.execute(
            "create temporary secret lake (type s3, key_id ?, secret ?, endpoint ?, "
            "use_ssl false, url_style 'path', region ?)",
            [
                connection_.login,
                connection_.password,
                endpoint,
                connection_.extra_dejson.get("region_name", "us-east-1"),
            ],
        )
        failures = 0
        for label in ("copy", "orphan"):
            batch_id, key = planted[label]
            (in_lake,) = connection.execute(
                f"select count(*) from read_parquet('s3://{bucket}/{key}')"
            ).fetchone()
            (in_model,) = connection.execute(
                f"select count(*) from bronze.br_{SOURCE_SYSTEM}__{ENTITY} "
                "where _object_batch_id = ?",
                [batch_id],
            ).fetchone()
            verdict = "absent from the model" if in_model == 0 else "IN THE MODEL"
            print(f"bronze-plant: {label}: {in_lake} row(s) in the lake, {verdict}")
            failures += in_lake == 0 or in_model != 0
    return 1 if failures else 0


def remove() -> int:
    planted = json.loads(STATE.read_text(encoding="utf-8"))
    client, bucket = clients.lake_client(), clients.lake_bucket()
    for label in ("copy", "orphan"):
        client.delete_object(Bucket=bucket, Key=planted[label][1])
    STATE.unlink()
    print("bronze-plant: planted objects removed")
    return 0


if __name__ == "__main__":
    actions = {"plant": plant, "verify": verify, "remove": remove}
    if len(sys.argv) != 2 or sys.argv[1] not in actions:
        raise SystemExit("usage: bronze_plant.py plant|verify|remove")
    raise SystemExit(actions[sys.argv[1]]())
