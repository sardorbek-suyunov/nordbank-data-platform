"""Unit tests for the plugin functions. No Airflow, no stack, no network."""

from __future__ import annotations

import pytest
from nordbank_ops import lake, source_db, warehouse

LOCK_MESSAGE = (
    'IO Error: Could not set lock on file "/opt/warehouse/nordbank.duckdb": '
    "Conflicting lock is held in /usr/bin/python3 (PID 4711)"
)


class FakeConnection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeS3:
    """Enough of the boto3 S3 client for the lake helpers."""

    def __init__(self, existing: dict[str, bytes] | None = None) -> None:
        self.objects: dict[str, bytes] = dict(existing or {})
        self.deleted: list[str] = []

    def put_object(self, Bucket: str, Key: str, Body: bytes) -> None:  # noqa: N803
        self.objects[Key] = Body

    def list_objects_v2(self, Bucket: str, Prefix: str) -> dict:  # noqa: N803
        contents = [{"Key": key} for key in sorted(self.objects) if key.startswith(Prefix)]
        return {"Contents": contents} if contents else {}

    def delete_object(self, Bucket: str, Key: str) -> None:  # noqa: N803
        self.objects.pop(Key, None)
        self.deleted.append(Key)


class FakeCursor:
    def __init__(self, rows: list[tuple], error: Exception | None = None) -> None:
        self._rows = rows
        self._error = error
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(statement)
        if self._error is not None and statement.lstrip().lower().startswith("create table"):
            raise self._error

    def fetchall(self) -> list[tuple]:
        return self._rows

    def fetchone(self) -> tuple:
        return self._rows[0]


class InsufficientPrivilegeError(Exception):
    pgcode = "42501"


def test_warehouse_path_requires_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DUCKDB_PATH", raising=False)
    with pytest.raises(RuntimeError, match="DUCKDB_PATH"):
        warehouse.warehouse_path()


def test_lock_conflict_is_recognised_and_the_holder_is_extracted() -> None:
    error = OSError(LOCK_MESSAGE)
    assert warehouse.is_lock_conflict(error)
    assert warehouse.lock_holder(error) == "4711"
    assert not warehouse.is_lock_conflict(OSError("disk full"))


def test_connect_retries_while_the_file_is_locked() -> None:
    attempts: list[int] = []
    delays: list[float] = []

    def opener(path: str, read_only: bool):
        attempts.append(1)
        if len(attempts) < 3:
            raise OSError(LOCK_MESSAGE)
        return FakeConnection()

    with warehouse.connect(
        path="/tmp/whatever.duckdb",
        opener=opener,
        sleep=delays.append,
        base_delay=0.1,
    ) as connection:
        assert isinstance(connection, FakeConnection)

    assert len(attempts) == 3
    assert delays == [0.1, 0.2], "backoff should double between attempts"


def test_connect_gives_up_and_names_the_holder() -> None:
    def opener(path: str, read_only: bool):
        raise OSError(LOCK_MESSAGE)

    with (
        pytest.raises(warehouse.WarehouseBusyError, match="pid 4711"),
        warehouse.connect(
            path="/tmp/whatever.duckdb", opener=opener, attempts=2, sleep=lambda _: None
        ),
    ):
        pass


def test_connect_does_not_retry_other_errors() -> None:
    def opener(path: str, read_only: bool):
        raise OSError("database file is corrupt")

    with (
        pytest.raises(OSError, match="corrupt"),
        warehouse.connect(path="/tmp/whatever.duckdb", opener=opener, sleep=lambda _: None),
    ):
        pass


def test_connect_closes_the_connection() -> None:
    connection = FakeConnection()

    with warehouse.connect(path="/tmp/whatever.duckdb", opener=lambda *_: connection):
        pass

    assert connection.closed


def test_initialise_creates_the_six_schemas_and_the_probe_table(tmp_path) -> None:
    duckdb = pytest.importorskip("duckdb")
    database = tmp_path / "warehouse.duckdb"

    with warehouse.connect(path=database) as connection:
        warehouse.initialise(connection)
        warehouse.initialise(connection)  # idempotent
        assert warehouse.missing_schemas(connection) == []
        connection.execute(
            f"insert into {warehouse.PROBE_TABLE} values ('p1', now(), 'test', 'detail')"
        )

    with duckdb.connect(str(database), read_only=True) as reader:
        rows = reader.execute(f"select probe_id from {warehouse.PROBE_TABLE}").fetchall()
    assert rows == [("p1",)]


def test_bronze_key_carries_the_batch_id() -> None:
    key = lake.bronze_key("corebank", "customers", "2026-09-18", "batch-1", part=2)
    assert key == (
        "bronze/corebank/customers/ingest_date=2026-09-18/batch_id=batch-1/part-0002.parquet"
    )


def test_two_batch_ids_under_one_ingest_date_produce_two_keys() -> None:
    client = FakeS3()
    result = lake.probe_bucket(client, "nordbank-lake", ("batch-a", "batch-b"), "2026-09-18")

    assert len(set(result["keys"])) == 2
    assert len(result["found"]) == 2
    assert client.objects == {}, "probe objects must be cleaned up"
    assert sorted(client.deleted) == sorted(result["keys"])


def test_probe_refuses_to_prove_nothing() -> None:
    with pytest.raises(ValueError, match="two different batch ids"):
        lake.probe_bucket(FakeS3(), "nordbank-lake", ("same", "same"), "2026-09-18")


def test_assert_prefixes_names_what_is_missing() -> None:
    client = FakeS3({"bronze/.keep": b""})
    with pytest.raises(AssertionError, match="quarantine"):
        lake.assert_prefixes(client, "nordbank-lake")


def test_source_schema_assertions() -> None:
    cursor = FakeCursor([("core",), ("ref",), ("public",)])
    assert source_db.assert_schemas(cursor) == ["core", "ref"]

    with pytest.raises(AssertionError, match="ref"):
        source_db.assert_schemas(FakeCursor([("core",)]))


def test_extraction_role_must_be_refused_a_create_table() -> None:
    refused = FakeCursor([], error=InsufficientPrivilegeError("permission denied for schema core"))
    assert source_db.assert_cannot_create(refused) == "42501"

    permitted = FakeCursor([])
    with pytest.raises(AssertionError, match="SELECT only"):
        source_db.assert_cannot_create(permitted)
