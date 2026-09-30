"""The bronze models are generated from the contracts, and the check fails when they disagree."""

import shutil
from pathlib import Path

import dbt_generate
import pytest
from nordbank_ops.tokenise import TOKEN_WIDTH

ROOT = Path(__file__).resolve().parents[2]
# Fifty contract entities at this commit: forty-five core banking, two clearing file entities,
# FX rates, the sanctions list and FRED. Stated, never read from the thing under test.
MODEL_COUNT = 50


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A copy of the contracts, the generated models and the inventory the generator can edit."""
    contracts = tmp_path / "contracts"
    models = tmp_path / "models"
    shutil.copytree(ROOT / "contracts", contracts)
    shutil.copytree(ROOT / "dbt" / "models" / "bronze", models)
    inventory = tmp_path / "model_inventory.md"
    shutil.copy(ROOT / "docs" / "model_inventory.md", inventory)
    monkeypatch.setattr(dbt_generate, "CONTRACTS", contracts)
    monkeypatch.setattr(dbt_generate, "MODELS", models)
    monkeypatch.setattr(dbt_generate, "INVENTORY", inventory)
    return tmp_path


def test_one_model_per_contract_entity():
    models = dbt_generate.build_models()
    assert len(models) == MODEL_COUNT
    assert len({m.name for m in models}) == MODEL_COUNT
    committed = sorted(p.stem for p in (ROOT / "dbt" / "models" / "bronze").glob("br_*.sql"))
    assert len(committed) == MODEL_COUNT
    assert committed == sorted(m.name for m in models)


def test_the_committed_files_agree_with_the_contracts():
    assert dbt_generate.main(["--check"]) == 0


def test_a_hand_edit_fails_the_check(workspace):
    target = workspace / "models" / "br_corebank__customers.yml"
    target.write_text(target.read_text(encoding="utf-8").replace("VARCHAR", "TEXT", 1))
    assert dbt_generate.main(["--check"]) == 1


def test_a_contract_changed_without_regenerating_fails_the_check(workspace):
    contract = workspace / "contracts" / "corebank" / "currencies.yml"
    text = contract.read_text(encoding="utf-8")
    contract.write_text(
        text.replace("classification: non-personal", "classification: sensitive", 1)
    )
    assert dbt_generate.main(["--check"]) == 1
    assert dbt_generate.main([]) == 0
    assert dbt_generate.main(["--check"]) == 0


def test_a_stray_model_file_fails_the_check(workspace):
    (workspace / "models" / "br_corebank__nothing.sql").write_text("select 1\n")
    assert dbt_generate.main(["--check"]) == 1


def test_identifier_columns_are_typed_and_tested_as_tokens():
    models = {m.name: m for m in dbt_generate.build_models()}
    cards = models["br_corebank__cards"]
    column = next(c for c in cards.columns if c.name == "card_reference")
    assert column.data_type == "VARCHAR"
    assert f"{TOKEN_WIDTH} lowercase hexadecimal" in column.description
    text = dbt_generate.render_yml(cards)
    assert f"width: {TOKEN_WIDTH}" in text


def test_a_retired_column_stays_and_is_described_by_delivery_date():
    models = {m.name: m for m in dbt_generate.build_models()}
    settlements = models["br_cardnet__settlements"]
    column = next(c for c in settlements.columns if c.name == "merchant_name")
    assert "null for deliveries from 2026-09-03" in column.description


def test_every_column_carries_its_classification_in_meta_and_description():
    for model in dbt_generate.build_models():
        text = dbt_generate.render_yml(model)
        assert text.count("classification:") >= len(model.columns)
        for column in model.columns:
            assert f"Classification: `{column.classification}`" in column.description


def test_every_model_has_the_grain_reconciliation_and_hive_key_tests():
    for model in dbt_generate.build_models():
        text = dbt_generate.render_yml(model)
        for test in (
            "bronze_unique_key",
            "bronze_not_null",
            "bronze_landing_reconciliation",
            "bronze_hive_key",
        ):
            assert test in text, (model.name, test)
        assert "store_failures" not in text


def test_the_inventory_section_lists_every_model_with_a_consumer():
    text = (ROOT / "docs" / "model_inventory.md").read_text(encoding="utf-8")
    section = text.split(dbt_generate.BEGIN, 1)[1].split(dbt_generate.END, 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("| `br_")]
    assert len(rows) == MODEL_COUNT
    assert all(not line.rstrip().endswith("|  |") for line in rows)
    assert any("br_cardnet__settlement_totals" in line for line in rows)
