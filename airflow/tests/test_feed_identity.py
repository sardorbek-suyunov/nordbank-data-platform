"""File identity by content checksum (spec 006 section 1, criterion 2, ADR 0013).

The object store is a small fake with the three calls discovery makes; the warehouse is a real
in-memory DuckDB with the schema applied, because the identity check is SQL.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb
import pytest
from nordbank_ops.feeds import identity

SCHEMA_DIR = Path(__file__).resolve().parent.parent.parent / "infra" / "warehouse" / "schema"
NOW = dt.datetime(2026, 9, 23, 9, 0, tzinfo=dt.UTC)
DAY = dt.date(2026, 8, 3)


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class FakeStore:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = dict(objects)

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):  # noqa: N803 - boto3 names
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        return {"Contents": [{"Key": k} for k in keys]}

    def get_object(self, Bucket, Key):  # noqa: N803 - boto3 names
        return {"Body": _Body(self.objects[Key])}


@pytest.fixture
def warehouse():
    connection = duckdb.connect(":memory:")
    for sql in sorted(SCHEMA_DIR.glob("*.sql")):
        code = "\n".join(
            line
            for line in sql.read_text(encoding="utf-8").splitlines()
            if not line.strip().startswith("--")
        )
        for statement in code.split(";"):
            if statement.strip():
                connection.execute(statement)
    yield connection
    connection.close()


FILE = b"H,NBK,2026-08-03,01\nrecord_type,transaction_reference\nD,TXN1\nZ,1\n"


def test_discovery_takes_deliveries_directly_under_the_prefix_only() -> None:
    store = FakeStore(
        {
            "cardnet/NBK_CLR_20260803_01.csv": FILE,
            "cardnet/_simulation/NBK_CLR_20260803_01.json": b"{}",
            "cardnet/readme.txt": b"not a delivery",
        }
    )
    found = identity.discover(store, "inbound", "cardnet/", ".csv")
    assert [c.key for c in found] == ["cardnet/NBK_CLR_20260803_01.csv"]
    assert found[0].checksum == identity.checksum(FILE)
    assert found[0].size == len(FILE)


def test_a_renamed_unchanged_file_has_the_same_identity() -> None:
    store = FakeStore({"cardnet/a.csv": FILE, "cardnet/renamed.csv": FILE})
    first, second = identity.discover(store, "inbound", "cardnet/", ".csv")
    assert first.key != second.key
    assert first.checksum == second.checksum


def test_one_changed_byte_is_a_different_file() -> None:
    assert identity.checksum(FILE) != identity.checksum(FILE.replace(b"TXN1", b"TXN2"))


def test_a_landed_checksum_is_recognised_and_cannot_land_twice(warehouse) -> None:
    candidate = identity.Candidate(key="cardnet/a.csv", checksum=identity.checksum(FILE), size=9)
    assert identity.landed_as(warehouse, [candidate.checksum]) == {}

    landed = identity.record_ingested(
        warehouse,
        checksum_=candidate.checksum,
        source_system="cardnet",
        entity="settlements",
        key=candidate.key,
        size=candidate.size,
        business_date=DAY,
        publisher_version=None,
        batch_id="settlements-20260803T000000-01",
        now=NOW,
    )
    assert landed is True
    assert identity.landed_as(warehouse, [candidate.checksum]) == {
        candidate.checksum: "settlements-20260803T000000-01"
    }

    again = identity.record_ingested(
        warehouse,
        checksum_=candidate.checksum,
        source_system="cardnet",
        entity="settlements",
        key="cardnet/renamed.csv",
        size=candidate.size,
        business_date=DAY,
        publisher_version=None,
        batch_id="settlements-20260803T000000-02",
        now=NOW,
    )
    assert again is False
    count = warehouse.execute("select count(*) from ops.ingested_file").fetchone()[0]
    assert count == 1


def test_a_sighting_records_what_discovery_concluded(warehouse) -> None:
    candidate = identity.Candidate(key="cardnet/renamed.csv", checksum="sha256:ab", size=9)
    identity.record_sighting(
        warehouse,
        candidate,
        source_system="cardnet",
        ingest_date=DAY,
        outcome=identity.ALREADY_INGESTED,
        batch_id="settlements-20260803T000000-01",
        run_id="manual__2026-08-03",
        now=NOW,
    )
    row = warehouse.execute(
        "select object_key, outcome, batch_id from ops.file_sighting"
    ).fetchone()
    assert row == ("cardnet/renamed.csv", "already_ingested", "settlements-20260803T000000-01")
