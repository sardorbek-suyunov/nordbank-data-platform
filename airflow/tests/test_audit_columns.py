"""The audit and quarantine column shapes, and the conventions table that states them (R21)."""

from __future__ import annotations

from pathlib import Path

from data_contract import AUDIT_COLUMNS, AUDIT_MEANING, PAYLOAD_COLUMN, QUARANTINE_COLUMNS

CONVENTIONS = Path(__file__).resolve().parent.parent.parent / "docs" / "conventions.md"
MODES = ("relational", "file", "snapshot", "api")


def test_every_audit_column_is_stated_for_every_mode_in_the_conventions() -> None:
    text = CONVENTIONS.read_text(encoding="utf-8")
    assert len(AUDIT_COLUMNS) >= 4
    assert set(AUDIT_MEANING) == set(AUDIT_COLUMNS)
    for column in AUDIT_COLUMNS:
        for mode in MODES:
            row = f"| `{column}` | {mode} | {AUDIT_MEANING[column][mode]} |"
            assert row in text, f"conventions.md does not state {column} for {mode} as code does"


def test_the_conventions_state_the_quarantine_record_shape() -> None:
    text = " ".join(CONVENTIONS.read_text(encoding="utf-8").split())
    assert len(QUARANTINE_COLUMNS) >= 9
    stated = ", ".join(f"`{c}`" for c in QUARANTINE_COLUMNS)
    assert stated in text and f"`{PAYLOAD_COLUMN}` for a file, snapshot or API source" in text
