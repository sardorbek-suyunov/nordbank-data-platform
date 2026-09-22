"""Unit tests for the data contract model.

The contract is what the ingest gate dispatches on: which columns are written to bronze, which
are tokenised, which column is the watermark and which is the key. A contract that parsed
loosely would be a gate that let something through, so every required field and every malformed
shape is tested rather than only the happy path.

No database and no Airflow: these run in `make test`.
"""

from __future__ import annotations

import pytest
from data_contract import (
    Contract,
    ContractColumn,
    ContractError,
    dump,
    load_all,
    parse,
)

MINIMAL = {
    "source_system": "corebank",
    "source_schema": "core",
    "entity": "accounts",
    "contract_version": 1,
    "watermark_column": "updated_at",
    "primary_key": "account_id",
    "dictionary_revision": "sha256:0123456789abcdef",
    "columns": [
        {
            "name": "account_id",
            "type": "bigint",
            "nullable": False,
            "classification": "pseudonymous_key",
        },
        {
            "name": "iban",
            "type": "character varying(34)",
            "nullable": True,
            "classification": "identifier",
        },
        {
            "name": "updated_at",
            "type": "timestamp with time zone",
            "nullable": False,
            "classification": "non-personal",
        },
    ],
}


def payload(**overrides):
    out = {key: value for key, value in MINIMAL.items()}
    out["columns"] = [dict(column) for column in MINIMAL["columns"]]
    out.update(overrides)
    return out


def test_parses_a_well_formed_contract():
    contract = parse(payload(), "test")
    assert contract.entity == "accounts"
    assert contract.column_names == ("account_id", "iban", "updated_at")
    assert contract.identifier_columns() == ("iban",)
    assert contract.qualified_relation == "core.accounts"


def test_column_lookup_returns_none_for_an_absent_column():
    assert parse(payload(), "test").column("nope") is None


@pytest.mark.parametrize("field", sorted(MINIMAL))
def test_every_required_field_is_required(field):
    body = payload()
    del body[field]
    with pytest.raises(ContractError, match=field):
        parse(body, "test")


def test_a_non_mapping_is_refused():
    with pytest.raises(ContractError, match="mapping at the top level"):
        parse(["not", "a", "mapping"], "test")


def test_an_empty_column_list_is_refused():
    with pytest.raises(ContractError, match="non-empty list"):
        parse(payload(columns=[]), "test")


def test_an_unknown_classification_is_refused():
    body = payload()
    body["columns"][1]["classification"] = "probably-fine"
    with pytest.raises(ContractError, match="probably-fine"):
        parse(body, "test")


def test_a_non_boolean_nullable_is_refused():
    body = payload()
    body["columns"][1]["nullable"] = "yes"
    with pytest.raises(ContractError, match="non-boolean"):
        parse(body, "test")


def test_a_missing_column_field_is_refused():
    body = payload()
    del body["columns"][0]["type"]
    with pytest.raises(ContractError, match="missing 'type'"):
        parse(body, "test")


def test_duplicate_column_names_are_refused():
    body = payload()
    body["columns"].append(dict(body["columns"][0]))
    with pytest.raises(ContractError, match="duplicate column name"):
        parse(body, "test")


@pytest.mark.parametrize("field", ["primary_key", "watermark_column"])
def test_the_key_and_the_watermark_must_be_described_columns(field):
    with pytest.raises(ContractError, match="declared but not described"):
        parse(payload(**{field: "absent_column"}), "test")


def test_the_fingerprint_changes_with_the_content_and_not_with_the_version():
    base = parse(payload(), "test")
    same_content_new_version = parse(payload(contract_version=2), "test")
    assert base.fingerprint == same_content_new_version.fingerprint

    body = payload()
    body["columns"][1]["nullable"] = False
    assert parse(body, "test").fingerprint != base.fingerprint


def test_a_contract_round_trips_through_its_rendered_form(tmp_path):
    contract = parse(payload(), "test")
    (tmp_path / "accounts.yml").write_text(dump(contract), encoding="utf-8")
    assert load_all(tmp_path)["accounts"] == contract


def test_two_entities_of_the_same_name_from_different_schemas_are_refused(tmp_path):
    core = parse(payload(), "test")
    other = Contract(
        source_system="corebank",
        source_schema="ref",
        entity="accounts",
        contract_version=1,
        watermark_column="updated_at",
        primary_key="account_id",
        dictionary_revision="sha256:0123456789abcdef",
        columns=(
            ContractColumn("account_id", "bigint", False, "non-personal"),
            ContractColumn("updated_at", "timestamp with time zone", False, "non-personal"),
        ),
    )
    (tmp_path / "a_accounts.yml").write_text(dump(core), encoding="utf-8")
    (tmp_path / "b_accounts.yml").write_text(dump(other), encoding="utf-8")
    with pytest.raises(ContractError, match="already contracted"):
        load_all(tmp_path)


def test_a_missing_contract_directory_is_refused_rather_than_empty(tmp_path):
    """The defect CI found on this milestone's pull request.

    The DAG factory resolved the contract root to a path that exists only inside the image.
    On a runner `load_all` returned nothing, two ingestion DAGs were built with no entities,
    no assets and nothing to do, and every DAG test but the asset count passed. A missing
    contract directory is a configuration failure and not an empty one.
    """
    with pytest.raises(ContractError, match="no such directory"):
        load_all(tmp_path / "not-here")
