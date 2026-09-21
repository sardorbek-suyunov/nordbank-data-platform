"""Unit tests for the contract-versus-dictionary diff, and for accepted drift.

The accepted-drift case is the one worth pinning. The dictionary records the source's
committed shape and a tick changes the live source without editing it, so once a breaking
drift is resolved by bumping a contract, that contract permanently disagrees with the
dictionary and the disagreement is correct. Without this, `CHECK=1` would fail for ever after
the first legitimate bump and the only way to make CI green again would be to remove the
check.

No database and no Airflow: these run in `make test`.
"""

from __future__ import annotations

import pytest
from contracts_diff import accepted_drift, differences
from data_contract import parse
from schema_contract import Column


def contract(**overrides):
    body = {
        "source_system": "corebank",
        "source_schema": "core",
        "entity": "payments",
        "contract_version": 1,
        "watermark_column": "updated_at",
        "primary_key": "payment_id",
        "dictionary_revision": "sha256:0123456789abcdef",
        "columns": [
            {
                "name": "payment_id",
                "type": "bigint",
                "nullable": False,
                "classification": "pseudonymous_key",
            },
            {
                "name": "remittance_reference",
                "type": "character varying(140)",
                "nullable": True,
                "classification": "non-personal",
            },
            {
                "name": "updated_at",
                "type": "timestamp with time zone",
                "nullable": False,
                "classification": "non-personal",
            },
        ],
    }
    body.update(overrides)
    return parse(body, "test")


def documented(**types):
    base = {
        "payment_id": "bigint",
        "remittance_reference": "character varying(140)",
        "updated_at": "timestamp with time zone",
    }
    base.update(types)
    nullable = {"payment_id": False, "remittance_reference": True, "updated_at": False}
    classification = {"payment_id": "pseudonymous_key"}
    return {
        name: Column(
            schema="core",
            table="payments",
            name=name,
            data_type=data_type,
            is_nullable=nullable[name],
            classification=classification.get(name, "non-personal"),
        )
        for name, data_type in base.items()
    }


def test_a_contract_matching_the_dictionary_has_no_findings():
    found, accepted = differences(contract(), documented())
    assert found == []
    assert accepted == []


def test_the_timeline_declares_what_each_entity_may_drift_to():
    assert accepted_drift(contract())["remittance_reference"][0] == "character varying(280)"


def test_a_contract_bumped_to_the_widened_type_is_accepted_drift_and_not_a_finding():
    body = contract().as_dict()
    body["contract_version"] = 2
    body["columns"][1]["type"] = "character varying(280)"
    found, accepted = differences(parse(body, "test"), documented())
    assert found == []
    assert len(accepted) == 1
    assert "payments_remittance_widened" in accepted[0]


def test_a_type_the_timeline_does_not_declare_is_a_finding():
    body = contract().as_dict()
    body["columns"][1]["type"] = "text"
    found, accepted = differences(parse(body, "test"), documented())
    assert accepted == []
    assert any("remittance_reference" in line for line in found)


def test_a_column_the_dictionary_lacks_is_a_finding_unless_a_drift_event_adds_it():
    body = contract().as_dict()
    body["columns"].append(
        {
            "name": "invented_column",
            "type": "text",
            "nullable": True,
            "classification": "non-personal",
        }
    )
    found, accepted = differences(parse(body, "test"), documented())
    assert accepted == []
    assert any("invented_column" in line for line in found)


def test_a_column_missing_from_the_contract_is_a_finding():
    body = contract().as_dict()
    body["columns"] = [c for c in body["columns"] if c["name"] != "remittance_reference"]
    found, _accepted = differences(parse(body, "test"), documented())
    assert any("not in the contract" in line for line in found)


@pytest.mark.parametrize(
    ("field", "value"),
    [("nullable", False), ("classification", "identifier")],
)
def test_nullability_and_classification_divergences_are_findings(field, value):
    body = contract().as_dict()
    body["columns"][1][field] = value
    found, _accepted = differences(parse(body, "test"), documented())
    assert len(found) == 1
