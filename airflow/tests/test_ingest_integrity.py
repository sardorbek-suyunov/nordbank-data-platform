"""The ingest-integrity checks can fail, on objects written the way the platform writes them."""

import decimal
import io
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from data_contract import load_history
from ingest_integrity import (
    identifier_columns,
    physical_schema,
    scan_objects,
    schemas_beyond_versions,
    unresolved_tokens,
)

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"
PREFIX = "bronze/corebank/customers/ingest_date=2026-07-20/batch_id=customers-20260720T000000-01/"


class Lake:
    """The two calls the checks make, over objects held in memory."""

    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    def list_objects_v2(self, Bucket, Prefix):  # noqa: N803 - boto3's spelling
        return {"Contents": [{"Key": k} for k in sorted(self.objects) if k.startswith(Prefix)]}

    def get_object(self, Bucket, Key):  # noqa: N803 - boto3's spelling
        return {"Body": io.BytesIO(self.objects[Key])}


def _parquet(columns: dict) -> bytes:
    buffer = io.BytesIO()
    pq.write_table(pa.table(columns), buffer, compression="snappy")
    return buffer.getvalue()


def _chains():
    chains = {}
    for directory in sorted(p for p in CONTRACTS.iterdir() if p.is_dir()):
        for entity, versions in load_history(directory).items():
            chains[(versions[-1].source_system, entity)] = versions
    return chains


def test_identifier_columns_come_from_every_contract_version():
    columns = identifier_columns(_chains())
    # Nine entities carry identifiers at this commit: eight in corebank and the clearing file.
    assert len(columns) >= 9
    assert ("cardnet", "settlements") in columns
    assert set(columns[("corebank", "customers")]) >= {"full_name", "email"}


def test_a_token_with_no_vault_row_is_reported():
    lake = Lake(
        {
            PREFIX + "part-0000.parquet": _parquet(
                {"customer_id": [1, 2, 3], "email": ["a" * 32, "b" * 32, "f" * 32]}
            )
        }
    )
    batches = [("corebank", "customers", PREFIX)]
    tokens, _ = scan_objects(lake, "lake", batches, {("corebank", "customers"): ("email",)})
    assert sum(len(v) for v in tokens.values()) >= 3

    unresolved = unresolved_tokens(tokens, vault={"a" * 32, "b" * 32})
    assert unresolved == {("corebank", "customers", "email"): {"f" * 32}}


def test_every_token_resolving_is_clean_and_nulls_are_not_tokens():
    lake = Lake({PREFIX + "part-0000.parquet": _parquet({"email": ["a" * 32, None]})})
    tokens, _ = scan_objects(
        lake, "lake", [("corebank", "customers", PREFIX)], {("corebank", "customers"): ("email",)}
    )
    assert tokens == {("corebank", "customers", "email"): {"a" * 32}}
    assert unresolved_tokens(tokens, vault={"a" * 32}) == {}


def test_a_column_an_older_object_does_not_carry_is_skipped_rather_than_failing():
    lake = Lake({PREFIX + "part-0000.parquet": _parquet({"email": ["a" * 32]})})
    wanted = {("corebank", "customers"): ("email", "national_identifier")}
    tokens, _ = scan_objects(lake, "lake", [("corebank", "customers", PREFIX)], wanted)
    assert set(tokens) == {("corebank", "customers", "email")}


def test_two_objects_of_one_version_with_inferred_types_are_two_schemas_and_fail_the_check():
    # What the writer did before it wrote the contract's schema: the type followed the values.
    second = PREFIX.replace("-01/", "-02/")
    lake = Lake(
        {
            PREFIX + "part-0000.parquet": _parquet({"amount": [decimal.Decimal("1.2500")]}),
            second + "part-0000.parquet": _parquet({"amount": [decimal.Decimal("12345.2500")]}),
        }
    )
    batches = [("corebank", "customers", PREFIX), ("corebank", "customers", second)]
    _, schemas = scan_objects(lake, "lake", batches, {})
    assert len(schemas[("corebank", "customers")]) == 2
    assert schemas_beyond_versions(schemas, {("corebank", "customers"): 1}) == {
        ("corebank", "customers"): (2, 1)
    }


def test_one_schema_per_version_is_clean():
    lake = Lake({PREFIX + "part-0000.parquet": _parquet({"email": ["a" * 32]})})
    _, schemas = scan_objects(lake, "lake", [("corebank", "customers", PREFIX)], {})
    assert schemas_beyond_versions(schemas, {("corebank", "customers"): 1}) == {}
    body = lake.objects[PREFIX + "part-0000.parquet"]
    assert physical_schema(pq.read_schema(io.BytesIO(body))) == (("email", "string"),)


def test_an_entity_with_objects_and_no_contract_is_beyond_its_zero_versions():
    lake = Lake({PREFIX + "part-0000.parquet": _parquet({"email": ["a" * 32]})})
    _, schemas = scan_objects(lake, "lake", [("corebank", "customers", PREFIX)], {})
    assert schemas_beyond_versions(schemas, {}) == {("corebank", "customers"): (1, 0)}
