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


# --- the sanctions excuse is scoped (specification 006 review, C6) ---------------------------

FIXTURE_NAME = "Zed Placeholder-Fixture"
SNAPSHOT_KEY = (
    "bronze/opensanctions/entities/ingest_date=2026-09-14/"
    "batch_id=entities-20260914T070000-01/part-0000.parquet"
)
PAYMENTS_KEY = (
    "bronze/corebank/payments/ingest_date=2026-09-14/"
    "batch_id=payments-20260913T000000-01/part-0000.parquet"
)


class _Store:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    def get_object(self, Bucket, Key):  # noqa: N803 - boto3's own spelling
        return {"Body": io.BytesIO(self.objects[f"{Bucket}/{Key}"])}


def _judge(key: str, delivered: bytes | None):
    from bronze_pii_scan import Matcher, judge

    store = _Store({"inbound/opensanctions/sanctions/20260914/entities.ftm.json": delivered or b""})
    deliveries = (
        {"entities-20260914T070000-01": "opensanctions/sanctions/20260914/entities.ftm.json"}
        if delivered is not None
        else {}
    )
    body = _parquet({"caption": [FIXTURE_NAME]})
    carried: set[str] = set()
    found = judge(
        store, "inbound", key, body, Matcher([("tok_fixture", encodings(FIXTURE_NAME))]),
        deliveries, carried,
    )  # fmt: skip
    return {token for token, _reading, _length in found}, carried


def test_a_list_name_in_its_own_snapshot_is_excused_when_the_delivery_carries_it():
    delivered = json.dumps({"caption": FIXTURE_NAME}).encode()
    assert _judge(SNAPSHOT_KEY, delivered) == (set(), {"tok_fixture"})


def test_a_list_name_planted_in_a_core_banking_object_is_caught():
    delivered = json.dumps({"caption": FIXTURE_NAME}).encode()
    assert _judge(PAYMENTS_KEY, delivered) == ({"tok_fixture"}, set())


def test_the_excuse_is_by_prefix_not_by_a_substring_anywhere_in_the_key():
    from bronze_pii_scan import in_sanctions_scope

    assert in_sanctions_scope(SNAPSHOT_KEY)
    assert in_sanctions_scope(SNAPSHOT_KEY.replace("bronze/", "quarantine/", 1))
    assert not in_sanctions_scope(PAYMENTS_KEY)
    assert not in_sanctions_scope("bronze/corebank/payments/x/opensanctions/part-0000.parquet")


def test_a_value_the_snapshots_delivery_does_not_carry_is_caught_in_the_snapshot():
    assert _judge(SNAPSHOT_KEY, b'{"caption": "somebody else"}') == ({"tok_fixture"}, set())


def test_a_snapshot_with_no_recorded_delivery_excuses_nothing():
    assert _judge(SNAPSHOT_KEY, None) == ({"tok_fixture"}, set())
