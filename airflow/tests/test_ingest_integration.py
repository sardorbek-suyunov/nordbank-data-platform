"""Ingestion against the running stack: a real source, a real lake, a real warehouse.

Marked `integration` and run inside a container by `make test-integration`, because the
warehouse file is on a named volume and the service names resolve only on the compose network.

**These call the phase bodies directly rather than through the scheduler**, and write into a
warehouse file of their own under `/tmp`. The scheduler-level properties — which task holds
which pool, the trigger rules, the assets — are asserted by the DAG integrity tests, and the
end-to-end path through the scheduler is what `make backfill` exercises. What is left for here
is the behaviour that needs a real source and a real lake and would be a fake testing a fake
anywhere else: the overlap window, the batch id cases, both drift behaviours and the
reconciliation.

Every object written lands under a prefix of this suite's own and is deleted afterwards, so a
run leaves the lake as it found it.
"""

from __future__ import annotations

import datetime as dt
import io
import os
import sys
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

sys.path.insert(0, "/opt/airflow/plugins")
sys.path.insert(0, "/opt/airflow")
sys.path.insert(0, "/opt/airflow/scripts")

from data_contract import Contract, ContractColumn, load_all  # noqa: E402
from nordbank_ops import clients, extract, register, registry, warehouse  # noqa: E402
from nordbank_ops.tokenise import Tokeniser  # noqa: E402
from nordbank_ops.validation import Rejection  # noqa: E402

CONTRACTS = Path("/opt/airflow/contracts/corebank")
SYSTEM = "corebank"
SALT = "integration-suite-salt"


@pytest.fixture(scope="module")
def contracts():
    if not CONTRACTS.is_dir():
        pytest.skip("contracts are not mounted")
    return load_all(CONTRACTS)


@pytest.fixture
def cursor():
    import psycopg2

    dsn = os.environ.get("AIRFLOW_CONN_NORDBANK_SOURCE_DB")
    if not dsn:
        pytest.skip("AIRFLOW_CONN_NORDBANK_SOURCE_DB is not set")
    try:
        connection = psycopg2.connect(dsn, connect_timeout=5)
    except psycopg2.OperationalError as exc:
        pytest.skip(f"source database unreachable: {exc}")
    try:
        with connection.cursor() as handle:
            yield handle
    finally:
        connection.rollback()
        connection.close()


@pytest.fixture
def lake():
    try:
        client = clients.lake_client()
        bucket = clients.lake_bucket()
        client.head_bucket(Bucket=bucket)
    except Exception as exc:  # noqa: BLE001 - a missing lake is a skip, not a failure
        pytest.skip(f"lake unreachable: {exc}")

    written: list[str] = []

    class Recording:
        def put_object(self, **kwargs):
            written.append(kwargs["Key"])
            return client.put_object(**kwargs)

        def get_object(self, **kwargs):
            return client.get_object(**kwargs)

    yield Recording(), bucket
    for key in written:
        client.delete_object(Bucket=bucket, Key=key)


@pytest.fixture
def warehouse_file(tmp_path):
    path = tmp_path / "integration.duckdb"
    schema_dir = Path("/opt/airflow/infra/warehouse/schema")
    if not schema_dir.is_dir():
        pytest.skip("the warehouse schema is not mounted")
    with warehouse.connect(read_only=False, path=path) as connection:
        for sql in sorted(schema_dir.glob("*.sql")):
            code = "\n".join(
                line
                for line in sql.read_text(encoding="utf-8").splitlines()
                if not line.strip().startswith("--")
            )
            for statement in code.split(";"):
                if statement.strip():
                    connection.execute(statement)
        yield connection


def tokeniser():
    return Tokeniser.from_environment({"PII_TOKEN_SALT": SALT})


def interval(day: int) -> dt.datetime:
    """An interval far outside anything the backfill uses, so nothing collides."""
    return dt.datetime(2030, 1, day, tzinfo=dt.UTC)


def open_batch(connection, contract, when, *, watermark_from=None, opened_at=None):
    key = registry.BatchKey(SYSTEM, contract.entity, when)
    allocation = registry.allocate(
        connection,
        key,
        source_schema=contract.source_schema,
        ingest_date=when.date(),
        interval_end=when + dt.timedelta(days=1),
        contract_version=contract.contract_version,
        watermark_from=watermark_from,
        opened_at=opened_at or dt.datetime.now(dt.UTC),
        triggering_run_id=f"test__{uuid.uuid4().hex[:8]}",
    )
    return allocation, registry.batch(connection, allocation.batch_id)


def run_extract(cursor, lake, contract, batch):
    client, bucket = lake
    return extract.extract_entity(
        cursor=cursor,
        client=client,
        bucket=bucket,
        contract=contract,
        batch=batch,
        tokeniser=tokeniser(),
    )


# --- one interval, end to end ---------------------------------------------------------------


def test_one_interval_lands_parquet_and_registers(contracts, cursor, lake, warehouse_file):
    contract = contracts["cards"]
    _allocation, batch = open_batch(warehouse_file, contract, interval(1))

    report = run_extract(cursor, lake, contract, batch)

    assert report.status == "written"
    assert report.rows_read > 0
    assert report.rows_landed + report.rows_quarantined == report.rows_read
    assert report.bronze_keys, "no bronze object was written"
    assert report.bronze_keys[0].startswith(
        f"bronze/corebank/cards/ingest_date={batch['ingest_date']}/"
    )

    registry.mark_written(
        warehouse_file,
        batch["batch_id"],
        rows_read=report.rows_read,
        rows_landed=report.rows_landed,
        rows_quarantined=report.rows_quarantined,
        watermark_to=report.watermark_to,
        written_at=dt.datetime.now(dt.UTC),
    )
    registry.mark_registered(warehouse_file, batch["batch_id"], dt.datetime.now(dt.UTC))
    assert registry.batch(warehouse_file, batch["batch_id"])["status"] == "registered"


# --- section 4: the idempotency cases --------------------------------------------------------


def test_a_retry_reuses_the_batch_id_and_produces_byte_identical_objects(
    contracts, cursor, lake, warehouse_file
):
    """Criterion 5. The retry must reproduce `_ingested_at`, which is why it is a batch column."""
    client, bucket = lake
    contract = contracts["cards"]
    first, batch = open_batch(warehouse_file, contract, interval(2))
    report = run_extract(cursor, lake, contract, batch)
    before = client.get_object(Bucket=bucket, Key=report.bronze_keys[0])["Body"].read()

    second, again = open_batch(warehouse_file, contract, interval(2))
    assert second.batch_id == first.batch_id
    assert second.reused is True
    repeat = run_extract(cursor, lake, contract, again)
    after = client.get_object(Bucket=bucket, Key=repeat.bronze_keys[0])["Body"].read()

    assert repeat.bronze_keys == report.bronze_keys
    assert after == before, "a retry produced different bytes for the same batch"


def test_a_rerun_after_registration_allocates_the_next_sequence(
    contracts, cursor, lake, warehouse_file
):
    """Criterion 6. The registered partition is not modified; a new one appears beside it."""
    client, bucket = lake
    contract = contracts["cards"]
    first, batch = open_batch(warehouse_file, contract, interval(3))
    report = run_extract(cursor, lake, contract, batch)
    registry.mark_written(
        warehouse_file,
        batch["batch_id"],
        rows_read=report.rows_read,
        rows_landed=report.rows_landed,
        rows_quarantined=report.rows_quarantined,
        watermark_to=report.watermark_to,
        written_at=dt.datetime.now(dt.UTC),
    )
    registry.mark_registered(warehouse_file, batch["batch_id"], dt.datetime.now(dt.UTC))
    registered_bytes = client.get_object(Bucket=bucket, Key=report.bronze_keys[0])["Body"].read()

    second, again = open_batch(warehouse_file, contract, interval(3))
    assert second.sequence == first.sequence + 1
    assert second.batch_id.endswith("-02")
    repeat = run_extract(cursor, lake, contract, again)
    assert repeat.bronze_keys[0] != report.bronze_keys[0]

    still = client.get_object(Bucket=bucket, Key=report.bronze_keys[0])["Body"].read()
    assert still == registered_bytes, "the registered partition was modified"


def test_registering_twice_is_refused(contracts, cursor, lake, warehouse_file):
    contract = contracts["cards"]
    _allocation, batch = open_batch(warehouse_file, contract, interval(4))
    report = run_extract(cursor, lake, contract, batch)
    registry.mark_written(
        warehouse_file,
        batch["batch_id"],
        rows_read=report.rows_read,
        rows_landed=report.rows_landed,
        rows_quarantined=report.rows_quarantined,
        watermark_to=report.watermark_to,
        written_at=dt.datetime.now(dt.UTC),
    )
    registry.mark_registered(warehouse_file, batch["batch_id"], dt.datetime.now(dt.UTC))
    with pytest.raises(registry.RegistryError, match="already registered"):
        registry.mark_registered(warehouse_file, batch["batch_id"], dt.datetime.now(dt.UTC))


# --- section 7: the overlap ------------------------------------------------------------------


def test_the_overlap_re_reads_rows_sharing_the_watermark_instant(
    contracts, cursor, lake, warehouse_file
):
    """Criterion 7, against the real source.

    `accounts` is the instrument that works every day: measured over sixteen `ci` ticks, the
    last jitter bucket of the movement phase puts rows on the daily maximum every time. The
    assertion is the property rather than a count, so it holds at any profile.
    """
    contract = contracts["accounts"]
    _first, batch = open_batch(warehouse_file, contract, interval(5))
    whole = run_extract(cursor, lake, contract, batch)
    if whole.watermark_to is None:
        pytest.skip("the source has no rows to watermark on")

    lag = dt.timedelta(minutes=15)
    _second, next_batch = open_batch(
        warehouse_file, contract, interval(6), watermark_from=whole.watermark_to - lag
    )
    overlap = run_extract(cursor, lake, contract, next_batch)

    cursor.execute(
        'select count(*) from core.accounts where "updated_at" = %s', (whole.watermark_to,)
    )
    at_the_instant = cursor.fetchone()[0]
    assert at_the_instant >= 1
    assert overlap.rows_read >= at_the_instant, (
        "the overlap did not re-read the rows sharing the watermark instant"
    )


# --- section 8: both drift behaviours ---------------------------------------------------------


def test_additive_drift_lands_the_batch_and_omits_the_column(
    contracts, cursor, lake, warehouse_file
):
    """Criterion 12, with the contract narrowed rather than the source widened.

    A contract that does not describe a column the source has is exactly what an additive
    drift event produces, and building it this way exercises the same path without waiting for
    the scripted event at anchor plus twelve.
    """
    import pyarrow.parquet as pq

    client, bucket = lake
    full = contracts["cards"]
    narrowed = Contract(
        source_system=full.source_system,
        source_schema=full.source_schema,
        entity=full.entity,
        contract_version=full.contract_version,
        watermark_column=full.watermark_column,
        primary_key=full.primary_key,
        dictionary_revision=full.dictionary_revision,
        columns=tuple(c for c in full.columns if c.name != "card_status_code"),
    )
    _allocation, batch = open_batch(warehouse_file, narrowed, interval(7))
    report = run_extract(cursor, lake, narrowed, batch)

    assert report.status == "written"
    kinds = {observation["kind"] for observation in report.drift}
    assert kinds == {"additive"}
    assert any(o["column"] == "card_status_code" for o in report.drift)

    body = client.get_object(Bucket=bucket, Key=report.bronze_keys[0])["Body"].read()
    written = pq.read_table(io.BytesIO(body))
    assert "card_status_code" not in written.column_names


def test_breaking_drift_fails_the_batch_and_lands_nothing(contracts, cursor, lake, warehouse_file):
    """Criterion 13, for the kind the scripted timeline reaches: a type change."""
    full = contracts["cards"]
    widened = Contract(
        source_system=full.source_system,
        source_schema=full.source_schema,
        entity=full.entity,
        contract_version=full.contract_version,
        watermark_column=full.watermark_column,
        primary_key=full.primary_key,
        dictionary_revision=full.dictionary_revision,
        columns=tuple(
            c
            if c.name != "card_reference"
            else ContractColumn(c.name, "text", c.is_nullable, c.classification)
            for c in full.columns
        ),
    )
    _allocation, batch = open_batch(warehouse_file, widened, interval(8))
    report = run_extract(cursor, lake, widened, batch)

    assert report.status == "failed"
    assert "type_changed" in report.failure_reason
    assert report.bronze_keys == []
    assert report.watermark_to is None

    registry.mark_failed(
        warehouse_file, batch["batch_id"], report.failure_reason, dt.datetime.now(dt.UTC)
    )
    assert registry.watermark(warehouse_file, SYSTEM, "cards") is None


# --- section 3: the watermark advances only on registration -------------------------------------


def test_a_failed_register_leaves_the_watermark_unmoved(contracts, cursor, lake, warehouse_file):
    """Criterion 4, by rolling the register transaction back the way a failure would."""
    contract = contracts["cards"]
    _allocation, batch = open_batch(warehouse_file, contract, interval(9))
    report = run_extract(cursor, lake, contract, batch)

    registry.mark_written(
        warehouse_file,
        batch["batch_id"],
        rows_read=report.rows_read,
        rows_landed=report.rows_landed,
        rows_quarantined=report.rows_quarantined,
        watermark_to=report.watermark_to,
        written_at=dt.datetime.now(dt.UTC),
    )

    warehouse_file.execute("begin transaction")
    registry.mark_registered(warehouse_file, batch["batch_id"], dt.datetime.now(dt.UTC))
    registry.advance_watermark(
        warehouse_file,
        SYSTEM,
        "cards",
        report.watermark_to,
        batch["batch_id"],
        dt.datetime.now(dt.UTC),
    )
    warehouse_file.execute("rollback")

    assert registry.batch(warehouse_file, batch["batch_id"])["status"] == "written"
    assert registry.watermark(warehouse_file, SYSTEM, "cards") is None


# --- section 6: the vault -----------------------------------------------------------------------


def test_the_vault_holds_one_row_per_value_across_entities(contracts, cursor, lake, warehouse_file):
    """Criteria 9 and 10, on the values the source really shares between two entities."""
    customers = contracts["customers"]
    payments = contracts["payments"]
    now = dt.datetime.now(dt.UTC)
    token = tokeniser()

    cursor.execute(
        """
        select c.full_name from core.customers c
          join core.payments p on p.counterparty_name = c.full_name
         limit 1
        """
    )
    shared = cursor.fetchone()
    if shared is None:
        pytest.skip("this book shares no name between customers and payments")
    name = shared[0]

    for contract, column in ((customers, "full_name"), (payments, "counterparty_name")):
        register.upsert_vault(
            warehouse_file,
            token,
            [name],
            source_system=SYSTEM,
            entity=contract.entity,
            column=column,
            batch_id=f"{contract.entity}-test",
            now=now,
        )

    rows = warehouse_file.execute(
        "select token, first_seen_entity, first_seen_column from meta.pii_vault "
        "where raw_value = ?",
        [name],
    ).fetchall()
    assert len(rows) == 1, "one raw value produced more than one vault row"
    assert rows[0][0] == token.token(name)
    assert (rows[0][1], rows[0][2]) == ("customers", "full_name")


# --- section 10: an empty reference batch ---------------------------------------------------------


def test_an_empty_reference_batch_registers_cleanly(contracts, cursor, lake, warehouse_file):
    """Criterion 21. Most days most reference batches are empty and that is a correct outcome."""
    contract = contracts["currencies"]
    far_future = dt.datetime(2099, 1, 1, tzinfo=dt.UTC)
    _allocation, batch = open_batch(
        warehouse_file, contract, interval(10), watermark_from=far_future
    )
    report = run_extract(cursor, lake, contract, batch)

    assert report.rows_read == 0
    assert report.rows_landed == 0
    assert report.bronze_keys == [], "an empty batch wrote an object"
    assert report.watermark_to is None

    registry.mark_written(
        warehouse_file,
        batch["batch_id"],
        rows_read=0,
        rows_landed=0,
        rows_quarantined=0,
        watermark_to=None,
        written_at=dt.datetime.now(dt.UTC),
    )
    registry.mark_registered(warehouse_file, batch["batch_id"], dt.datetime.now(dt.UTC))
    assert registry.batch(warehouse_file, batch["batch_id"])["status"] == "registered"


# --- section 7: an injected violation, through the real path -------------------------------


def test_an_injected_violation_quarantines_real_records(contracts, cursor, lake, warehouse_file):
    """Criterion 11, against rows the source really holds.

    Injection is the only quarantine evidence at this specification and the reason is
    structural: a source behind foreign keys, check constraints and not-null constraints
    cannot produce a malformed record. The injection here is a contract that declares
    `core.customers.email` non-nullable — which no committed contract does, for the reason in
    `docs/architecture.md` — so the rows the dirt phase has nulled are refused by the real
    gate, written to the real quarantine prefix and indexed in `dq.quarantine_log`.
    """
    full = contracts["customers"]
    strict = Contract(
        source_system=full.source_system,
        source_schema=full.source_schema,
        entity=full.entity,
        contract_version=full.contract_version,
        watermark_column=full.watermark_column,
        primary_key=full.primary_key,
        dictionary_revision=full.dictionary_revision,
        columns=tuple(
            c if c.name != "email" else ContractColumn(c.name, c.data_type, False, c.classification)
            for c in full.columns
        ),
    )
    cursor.execute("select count(*) from core.customers where email is null")
    nulled = cursor.fetchone()[0]
    if nulled == 0:
        pytest.skip("this book has no customer with a cleared email")

    _allocation, batch = open_batch(warehouse_file, strict, interval(11))
    report = run_extract(cursor, lake, strict, batch)

    assert report.rows_quarantined == nulled
    assert report.rows_landed + report.rows_quarantined == report.rows_read
    assert report.quarantine_keys, "nothing was written to the quarantine prefix"

    client, bucket = lake
    loaded = register.load_quarantine(warehouse_file, client, bucket, report.quarantine_keys)
    assert loaded == nulled

    reasons = warehouse_file.execute(
        "select distinct column_name, reason, value_is_tokenised from dq.quarantine_log"
    ).fetchall()
    assert reasons == [("email", "null in a non-nullable column", True)]


def test_a_quarantined_identifier_is_stored_as_a_token(contracts, cursor, lake, warehouse_file):
    """Criterion 11's second half, on a value rather than on a null.

    Quarantine sits beside bronze rather than inside it, so a cleartext identifier here would
    be personal data in a place the vault does not cover. The offending value of an identifier
    column is therefore the token.
    """
    rejected = [
        ({"customer_id": 1}, Rejection("email", "type", "someone@example.invalid", 1)),
    ]
    produced = extract.quarantine_rows(
        rejected,
        tokeniser(),
        ("email",),
        batch_id="customers-test",
        system=SYSTEM,
        entity="customers",
        quarantined_at=dt.datetime.now(dt.UTC),
    )
    assert produced[0]["value_is_tokenised"] is True
    assert produced[0]["offending_value"] == tokeniser().token("someone@example.invalid")
    assert "someone@example.invalid" not in str(produced[0])
