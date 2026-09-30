"""The writer writes the contract's schema, not one inferred from values (spec 007 section 1)."""

import datetime as dt
import decimal
import io
import json
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from data_contract import ContractError, load_history, type_family
from nordbank_ops.physical import arrow_type, bronze_schema, quarantine_schema, table

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"


def _versions():
    out = []
    for directory in sorted(p for p in CONTRACTS.iterdir() if p.is_dir()):
        for versions in load_history(directory).values():
            out.extend(versions)
    return out


def _bytes(rows, schema) -> bytes:
    buffer = io.BytesIO()
    pq.write_table(table(rows, schema), buffer, compression="snappy")
    return buffer.getvalue()


def test_every_contract_version_has_a_bronze_and_a_quarantine_schema():
    versions = _versions()
    # Fifty entities, two of them with a second version, at this commit.
    assert len({(v.source_system, v.entity) for v in versions}) >= 50
    assert len(versions) >= 52
    for version in versions:
        assert bronze_schema(version).names == list(version.bronze_columns)
        assert quarantine_schema(version).names == list(version.quarantine_columns)


def test_the_type_follows_the_contract_not_the_values():
    schema = pa.schema(
        [
            pa.field("amount", arrow_type("numeric(18,4)")),
            pa.field("closed_date", arrow_type("date")),
            pa.field("n", arrow_type("smallint")),
        ]
    )
    narrow = [{"amount": decimal.Decimal("1.2500"), "closed_date": None, "n": 1}]
    wide = [{"amount": decimal.Decimal("123456789.2500"), "closed_date": dt.date(2026, 7, 20)}]
    first = pq.read_schema(io.BytesIO(_bytes(narrow, schema)))
    second = pq.read_schema(io.BytesIO(_bytes(wide, schema)))
    # Inferred, these were decimal128(5, 4) against decimal128(13, 4), and null against date32.
    assert first.equals(second)
    assert first.field("amount").type == pa.decimal128(18, 4)
    assert first.field("closed_date").type == pa.date32()
    assert first.field("n").type == pa.int16()


def test_a_value_that_does_not_fit_its_declared_type_raises_rather_than_widening():
    schema = pa.schema([pa.field("amount", arrow_type("numeric(18,4)"))])
    with pytest.raises((pa.ArrowInvalid, pa.ArrowTypeError)):
        _bytes([{"amount": "not a number"}], schema)
    with pytest.raises(pa.ArrowInvalid):
        _bytes([{"amount": decimal.Decimal("1.23456")}], schema)


def test_a_json_column_is_json_text_and_duckdb_reads_it_as_json(tmp_path):
    schema = pa.schema([pa.field("names", arrow_type("json"))])
    rows = [{"names": ["Zoë Tester", "Z. Tester"]}, {"names": None}]
    path = tmp_path / "a.parquet"
    path.write_bytes(_bytes(rows, schema))
    values = pq.read_table(path).column("names").to_pylist()
    assert json.loads(values[0]) == ["Zoë Tester", "Z. Tester"] and values[1] is None
    connection = duckdb.connect()
    (declared,) = connection.execute(
        f"select typeof(names) from read_parquet('{path.as_posix()}') limit 1"
    ).fetchone()
    assert declared == "JSON"


def test_a_timestamp_with_an_offset_is_written_in_utc():
    schema = pa.schema([pa.field("at", arrow_type("timestamp with time zone"))])
    at = dt.datetime(2026, 7, 20, 6, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))
    written = pq.read_table(io.BytesIO(_bytes([{"at": at}], schema))).column("at")[0].as_py()
    assert written == dt.datetime(2026, 7, 20, 4, 0, tzinfo=dt.UTC)


def test_every_declared_type_in_the_contracts_has_a_family_and_an_unknown_one_is_refused():
    declared = {c.data_type for v in _versions() for c in v.columns}
    assert len(declared) >= 20
    for spelling in declared:
        arrow_type(spelling)
    with pytest.raises(ContractError):
        type_family("money")
    with pytest.raises(ContractError):
        type_family("numeric")
