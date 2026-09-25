"""The bronze PII scan finds a value in every form an object can hold it (spec 006 criterion 12)."""

import io
import json

import pyarrow as pa
import pyarrow.parquet as pq
from bronze_pii_scan import encodings, find

# Card-reference-shaped values that share long prefixes, as a column of them does.
VALUES = [f"C{100000 + i * 7:011d}" for i in range(400)]


def _parquet(columns: dict, compression: str = "snappy") -> bytes:
    buffer = io.BytesIO()
    pq.write_table(pa.table(columns), buffer, compression=compression)
    return buffer.getvalue()


def test_a_byte_wise_search_misses_values_in_a_snappy_column_and_the_decoded_reading_does_not():
    assert len(VALUES) >= 400
    body = _parquet({"card_reference": VALUES})
    planted = VALUES[200]
    hits = find(body, [("tok", encodings(planted))])
    readings = {reading for _token, reading, _length in hits}
    assert "decoded" in readings
    # The reason the decoded reading exists: the compressed bytes do not hold most values.
    assert sum(value.encode() in body for value in VALUES) < len(VALUES) // 10


def test_a_value_padded_in_the_vault_is_found_trimmed_in_an_object():
    body = _parquet({"card_reference": ["C00000000001"]})
    assert find(body, [("tok", encodings("C00000000001   "))])


def test_a_value_is_found_as_json_escapes_it_in_a_payload():
    name = 'Zoë "ZZ" Tester'
    payload = json.dumps({"name": name})
    body = _parquet({"_raw_payload": [payload]}, compression="none")
    assert name.encode() not in body
    assert {r for _t, r, _n in find(body, [("tok", encodings(name))])} == {"bytes", "decoded"}


def test_a_value_is_found_as_csv_quotes_it_in_a_payload():
    value = 'Acme "North" Ltd'
    payload = f'D,1,"{value.replace(chr(34), chr(34) * 2)}",EUR'
    body = _parquet({"_raw_payload": [payload]})
    assert find(body, [("tok", encodings(value))])


def test_an_absent_value_is_not_found():
    body = _parquet({"card_reference": VALUES})
    assert find(body, [("tok", encodings("C99999999999"))]) == []
