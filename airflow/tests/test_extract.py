"""Unit tests for the extract phase: the window, the Parquet write and the audit columns.

The source and the lake are both faked here, because what is under test is the arithmetic of
the window and the shape of what gets written, not psycopg or MinIO. The end-to-end behaviour
is asserted against the real stack in the integration suite.

No database and no Airflow: these run in `make test`.
"""

from __future__ import annotations

import datetime as dt
import io

import pyarrow.parquet as pq
import pytest
from data_contract import parse
from nordbank_ops.extract import (
    AUDIT_COLUMNS,
    QUARANTINE_COLUMNS,
    decorate,
    extract_entity,
    quarantine_rows,
    read_window,
    write_parquet,
)
from nordbank_ops.tokenise import Tokeniser
from nordbank_ops.validation import QuarantineReason, Rejection

SALT = "a-test-salt-that-is-not-the-real-one"
OPENED_AT = dt.datetime(2026, 9, 21, 4, 0, tzinfo=dt.UTC)

CONTRACT = parse(
    {
        "source_system": "corebank",
        "source_schema": "core",
        "entity": "customers",
        "contract_version": 1,
        "watermark_column": "updated_at",
        "primary_key": "customer_id",
        "dictionary_revision": "sha256:0123456789abcdef",
        "columns": [
            {
                "name": "customer_id",
                "type": "bigint",
                "nullable": False,
                "classification": "pseudonymous_key",
            },
            {
                "name": "full_name",
                "type": "character varying(200)",
                "nullable": False,
                "classification": "identifier",
            },
            {
                "name": "city",
                "type": "character varying(120)",
                "nullable": True,
                "classification": "quasi-identifier",
            },
            {
                "name": "updated_at",
                "type": "timestamp with time zone",
                "nullable": False,
                "classification": "non-personal",
            },
        ],
    },
    "test",
)

STAMP = dt.datetime(2026, 9, 19, 23, 45, tzinfo=dt.UTC)
EARLIER = dt.datetime(2026, 9, 19, 2, 0, tzinfo=dt.UTC)


class FakeCursor:
    """Answers the three shapes of query the extract phase issues."""

    def __init__(self, rows, types=None, primary_key="customer_id", identifiers=("full_name",)):
        self.rows = rows
        self.types = types or {c.name: c.data_type for c in CONTRACT.columns}
        self.primary_key = primary_key
        self.identifiers = identifiers
        self.description = None
        self._result: list = []
        self.executed: list[tuple[str, tuple]] = []

    def execute(self, sql, parameters=()):
        self.executed.append((sql, parameters))
        lowered = " ".join(sql.split()).lower()
        if "pg_attribute" in lowered and "format_type" in lowered:
            self._result = list(self.types.items())
        elif "indisprimary" in lowered:
            self._result = [] if self.primary_key is None else [(self.primary_key,)]
        elif "column_classifications" in lowered:
            self._result = [(name,) for name in self.identifiers]
        else:
            selected = self.rows
            if parameters:
                bound = parameters[0]
                selected = [r for r in self.rows if r["updated_at"] >= bound]
            names = tuple(CONTRACT.column_names)
            self.description = [(name,) for name in names]
            self._result = [tuple(row[name] for name in names) for row in selected]

    def fetchall(self):
        return self._result


class FakeLake:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put_object(self, Bucket, Key, Body):  # noqa: N803 - boto3's own spelling
        self.objects[Key] = Body


def rows():
    return [
        {
            "customer_id": 1,
            "full_name": "Jan Kowalski",
            "city": "Berlin",
            "updated_at": EARLIER,
        },
        {
            "customer_id": 2,
            "full_name": "Ada Nowak",
            "city": None,
            "updated_at": STAMP,
        },
        {
            "customer_id": 3,
            "full_name": "Ada Nowak",
            "city": "Kraków",
            "updated_at": STAMP,
        },
    ]


def batch(**overrides):
    body = {
        "batch_id": "customers-20260919T000000-01",
        "ingest_date": dt.date(2026, 9, 21),
        "opened_at": OPENED_AT,
        "watermark_from": None,
    }
    body.update(overrides)
    return body


def tokeniser() -> Tokeniser:
    return Tokeniser.from_environment({"PII_TOKEN_SALT": SALT})


# --- the window --------------------------------------------------------------------------


def test_the_window_reads_everything_when_there_is_no_watermark():
    read, observed = read_window(FakeCursor(rows()), CONTRACT, None)
    assert len(read) == 3
    assert observed == STAMP


def test_the_window_is_inclusive_of_its_lower_bound():
    """`>=`, so rows sharing the instant that became the watermark are re-read, not lost."""
    read, observed = read_window(FakeCursor(rows()), CONTRACT, STAMP)
    assert [row["customer_id"] for row in read] == [2, 3]
    assert observed == STAMP


def test_the_window_is_ordered_by_the_primary_key():
    cursor = FakeCursor(rows())
    read_window(cursor, CONTRACT, None)
    statement = cursor.executed[-1][0]
    assert 'order by "customer_id"' in statement


def test_an_empty_window_observes_no_maximum():
    read, observed = read_window(FakeCursor([]), CONTRACT, None)
    assert read == []
    assert observed is None


# --- the write ----------------------------------------------------------------------------


def test_an_empty_batch_writes_no_object():
    lake = FakeLake()
    assert write_parquet(lake, "bucket", "k", [], CONTRACT.column_names) is None
    assert lake.objects == {}


def test_the_audit_columns_are_the_four_the_conventions_name():
    assert AUDIT_COLUMNS == ("_ingested_at", "_source_file", "_batch_id", "_source_system")


def test_decorate_stamps_the_batch_and_not_the_clock():
    decorated = decorate(
        [{"customer_id": 1}],
        ingested_at=OPENED_AT,
        source_file="core.customers",
        batch_id="b",
        system="corebank",
    )
    assert decorated[0]["_ingested_at"] == OPENED_AT
    assert decorated[0]["_source_file"] == "core.customers"


def test_quarantine_tokenises_an_identifier_and_leaves_other_columns_alone():
    rejected = [
        ({"customer_id": 1}, Rejection("full_name", QuarantineReason.TYPE_MISMATCH, 7, 1)),
        ({"customer_id": 2}, Rejection("city", QuarantineReason.TYPE_MISMATCH, 9, 2)),
    ]
    produced = quarantine_rows(
        rejected,
        tokeniser(),
        ("full_name",),
        batch_id="b",
        system="corebank",
        entity="customers",
        quarantined_at=OPENED_AT,
    )
    assert produced[0]["value_is_tokenised"] is True
    assert produced[0]["offending_value"] == tokeniser().token(7)
    assert produced[1]["value_is_tokenised"] is False
    assert produced[1]["offending_value"] == "9"
    assert tuple(produced[0]) == QUARANTINE_COLUMNS


# --- the phase -----------------------------------------------------------------------------


def landed_table(lake: FakeLake):
    key = next(k for k in lake.objects if k.startswith("bronze/"))
    return pq.read_table(io.BytesIO(lake.objects[key])).to_pylist()


def test_the_phase_writes_tokenised_rows_with_their_audit_columns():
    lake = FakeLake()
    report = extract_entity(
        cursor=FakeCursor(rows()),
        client=lake,
        bucket="bucket",
        contract=CONTRACT,
        batch=batch(),
        tokeniser=tokeniser(),
    )
    assert report.status == "written"
    assert (report.rows_read, report.rows_landed, report.rows_quarantined) == (3, 3, 0)
    assert report.watermark_to == STAMP

    written = landed_table(lake)
    assert len(written) == 3
    assert written[0]["full_name"] == tokeniser().token("Jan Kowalski")
    assert written[0]["city"] == "Berlin"
    assert written[0]["_batch_id"] == "customers-20260919T000000-01"
    assert written[0]["_source_file"] == "core.customers"


def test_the_same_name_in_two_rows_tokenises_to_one_token():
    lake = FakeLake()
    extract_entity(
        cursor=FakeCursor(rows()),
        client=lake,
        bucket="bucket",
        contract=CONTRACT,
        batch=batch(),
        tokeniser=tokeniser(),
    )
    written = landed_table(lake)
    assert written[1]["full_name"] == written[2]["full_name"]


def test_no_cleartext_identifier_reaches_the_object():
    lake = FakeLake()
    extract_entity(
        cursor=FakeCursor(rows()),
        client=lake,
        bucket="bucket",
        contract=CONTRACT,
        batch=batch(),
        tokeniser=tokeniser(),
    )
    body = next(v for k, v in lake.objects.items() if k.startswith("bronze/"))
    assert b"Jan Kowalski" not in body
    assert b"Ada Nowak" not in body
    # A quasi-identifier is deliberately in the clear, so it must still be there.
    assert b"Berlin" in body


def test_additive_drift_lands_the_batch_and_omits_the_column():
    types = {c.name: c.data_type for c in CONTRACT.columns}
    types["merchant_risk_score"] = "numeric(18,8)"
    lake = FakeLake()
    report = extract_entity(
        cursor=FakeCursor(rows(), types=types),
        client=lake,
        bucket="bucket",
        contract=CONTRACT,
        batch=batch(),
        tokeniser=tokeniser(),
    )
    assert report.status == "written"
    assert [o["kind"] for o in report.drift] == ["additive"]
    assert "merchant_risk_score" not in landed_table(lake)[0]


def test_breaking_drift_fails_the_batch_and_writes_nothing():
    types = {c.name: c.data_type for c in CONTRACT.columns}
    types["full_name"] = "character varying(400)"
    lake = FakeLake()
    report = extract_entity(
        cursor=FakeCursor(rows(), types=types),
        client=lake,
        bucket="bucket",
        contract=CONTRACT,
        batch=batch(),
        tokeniser=tokeniser(),
    )
    assert report.status == "failed"
    assert "type_changed" in report.failure_reason
    assert report.watermark_to is None
    assert lake.objects == {}


def test_a_quarantined_record_is_written_to_the_quarantine_prefix():
    bad = rows()
    bad[0]["full_name"] = None  # non-nullable
    lake = FakeLake()
    report = extract_entity(
        cursor=FakeCursor(bad),
        client=lake,
        bucket="bucket",
        contract=CONTRACT,
        batch=batch(),
        tokeniser=tokeniser(),
    )
    assert (report.rows_read, report.rows_landed, report.rows_quarantined) == (3, 2, 1)
    assert report.rows_landed + report.rows_quarantined == report.rows_read
    assert any(key.startswith("quarantine/") for key in lake.objects)


def test_the_batch_report_carries_no_row_data():
    """Nothing this phase hands to the next may contain a source value."""
    lake = FakeLake()
    report = extract_entity(
        cursor=FakeCursor(rows()),
        client=lake,
        bucket="bucket",
        contract=CONTRACT,
        batch=batch(),
        tokeniser=tokeniser(),
    )
    rendered = repr(report.as_dict())
    assert "Jan Kowalski" not in rendered
    assert "Berlin" not in rendered


@pytest.mark.parametrize("primary_key", [None, "full_name"])
def test_a_primary_key_the_contract_does_not_declare_is_handled(primary_key):
    """None means the key could not be read and is not a change; a different one is breaking."""
    lake = FakeLake()
    report = extract_entity(
        cursor=FakeCursor(rows(), primary_key=primary_key),
        client=lake,
        bucket="bucket",
        contract=CONTRACT,
        batch=batch(),
        tokeniser=tokeniser(),
    )
    assert report.status == ("written" if primary_key is None else "failed")
