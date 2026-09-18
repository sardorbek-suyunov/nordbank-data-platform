"""Unit tests for the data dictionary parser and the type canonicaliser.

The parser is on the critical path for three acceptance criteria: it generates
`platform.column_classifications`, it drives `make schema-check`, and it is what says the
dictionary is column-complete. A parser that accepts malformed input quietly would under-
classify columns, and an under-classified column is one the extraction layer will not tokenise.
So the failure cases are tested as carefully as the success case.

No database and no Airflow: these run in `make test`.
"""

from __future__ import annotations

import pytest
from schema_contract import (
    COLUMN_TABLE_HEADER,
    DictionaryError,
    canonical_types,
    compare,
    live_columns,
    parse_dictionary,
)
from schema_contract import Column as SchemaColumn

HEADER = "| " + " | ".join(COLUMN_TABLE_HEADER) + " |"
SEPARATOR = "|---|---|---|---|---|---|"


def dictionary(*rows: str, heading: str = "#### core.customers") -> str:
    return "\n".join([heading, "", HEADER, SEPARATOR, *rows, ""])


GOOD_ROW = "| `customer_id` | bigint | no | `non-personal` | Surrogate primary key. | - |"


def test_parses_a_well_formed_table() -> None:
    columns = parse_dictionary(dictionary(GOOD_ROW))

    assert len(columns) == 1
    column = columns[0]
    assert column.schema == "core"
    assert column.table == "customers"
    assert column.name == "customer_id"
    assert column.data_type == "bigint"
    assert column.is_nullable is False
    assert column.classification == "non-personal"
    assert column.qualified_name == "core.customers.customer_id"


def test_ignores_prose_and_tables_outside_a_schema_heading() -> None:
    text = "\n".join(
        [
            "# Data dictionary",
            "",
            "| Entity | Domain |",
            "|---|---|",
            "| customers | Customer |",
            "",
            dictionary(GOOD_ROW),
        ]
    )

    assert [column.name for column in parse_dictionary(text)] == ["customer_id"]


def test_a_wrong_header_is_an_error() -> None:
    text = "\n".join(
        [
            "#### core.customers",
            "",
            "| Column | Type | Nullable | Classification | Description |",
            "|---|---|---|---|---|",
            "| `customer_id` | bigint | no | `non-personal` | Surrogate primary key. |",
        ]
    )

    with pytest.raises(DictionaryError) as error:
        parse_dictionary(text)

    assert "header is" in str(error.value)
    assert error.value.line_number == 3


def test_a_missing_column_in_a_row_is_an_error() -> None:
    with pytest.raises(DictionaryError) as error:
        parse_dictionary(dictionary("| `customer_id` | bigint | no | `non-personal` | Key. |"))

    assert "5 cells, expected 6" in str(error.value)


def test_a_duplicate_row_is_an_error() -> None:
    with pytest.raises(DictionaryError) as error:
        parse_dictionary(dictionary(GOOD_ROW, GOOD_ROW))

    assert "already documented at line" in str(error.value)


def test_an_unknown_classification_is_an_error() -> None:
    row = "| `customer_id` | bigint | no | `secret` | Surrogate primary key. | - |"

    with pytest.raises(DictionaryError) as error:
        parse_dictionary(dictionary(row))

    assert "unknown classification" in str(error.value)


def test_a_missing_description_is_an_error() -> None:
    row = "| `customer_id` | bigint | no | `non-personal` |  | - |"

    with pytest.raises(DictionaryError) as error:
        parse_dictionary(dictionary(row))

    assert "has no description" in str(error.value)


def test_a_nullable_that_is_not_yes_or_no_is_an_error() -> None:
    row = "| `customer_id` | bigint | maybe | `non-personal` | Key. | - |"

    with pytest.raises(DictionaryError) as error:
        parse_dictionary(dictionary(row))

    assert "expected yes or no" in str(error.value)


def test_a_type_that_is_not_a_plain_type_name_is_rejected() -> None:
    row = "| `customer_id` | bigint; drop table core.customers | no | `non-personal` | Key. | - |"

    with pytest.raises(DictionaryError) as error:
        parse_dictionary(dictionary(row))

    assert "is not a plain type name" in str(error.value)


def test_a_heading_with_no_table_is_an_error() -> None:
    with pytest.raises(DictionaryError) as error:
        parse_dictionary("#### core.customers\n\nSome prose and no table.\n")

    assert "has no column table" in str(error.value)


def test_a_dictionary_with_no_column_tables_is_an_error() -> None:
    with pytest.raises(DictionaryError) as error:
        parse_dictionary("# Data dictionary\n\nNothing here.\n")

    assert "no column tables found" in str(error.value)


def test_error_carries_the_source_the_line_number_and_the_line() -> None:
    with pytest.raises(DictionaryError) as error:
        parse_dictionary(dictionary("| `a` | bigint | no | `wrong` | Text. | - |"), source="d.md")

    assert error.value.source == "d.md"
    assert error.value.line_number == 5
    assert "`wrong`" in error.value.line
    assert "d.md:5" in str(error.value)


class FakeExecutor:
    """Stands in for the database so the comparison logic is testable without one."""

    def __init__(self, canonical: dict[str, str]) -> None:
        self.canonical = canonical
        self.queries: list[str] = []

    def __call__(self, sql: str) -> list[tuple[str, ...]]:
        self.queries.append(sql)
        return [(documented, canonical) for documented, canonical in self.canonical.items()]


def test_canonical_types_asks_once_per_distinct_string() -> None:
    executor = FakeExecutor({"decimal(18,4)": "numeric(18,4)"})

    resolved = canonical_types(executor, ["decimal(18,4)"] * 20)

    assert resolved == {"decimal(18,4)": "numeric(18,4)"}
    assert len(executor.queries) == 1


def test_canonical_types_rejects_a_type_string_that_is_not_a_plain_name() -> None:
    with pytest.raises(ValueError, match="not a plain type name"):
        canonical_types(FakeExecutor({}), ["numeric(18,4); drop table core.customers"])


def test_canonical_types_rejects_a_type_postgres_does_not_recognise() -> None:
    with pytest.raises(ValueError, match="does not recognise"):
        canonical_types(FakeExecutor({"nosuchtype": ""}), ["nosuchtype"])


def test_compare_accepts_an_alias_that_resolves_to_the_live_type() -> None:
    documented = [SchemaColumn("core", "t", "c", "decimal(18,4)", False, "non-personal", "x")]
    live = [SchemaColumn("core", "t", "c", "numeric(18,4)", False)]

    assert compare(documented, live, FakeExecutor({"decimal(18,4)": "numeric(18,4)"})) == []


def test_compare_reports_a_real_type_difference() -> None:
    documented = [SchemaColumn("core", "t", "c", "numeric(18,8)", False, "non-personal", "x")]
    live = [SchemaColumn("core", "t", "c", "numeric(18,4)", False)]

    differences = compare(documented, live, FakeExecutor({"numeric(18,8)": "numeric(18,8)"}))

    assert [d.kind for d in differences] == ["type disagrees"]


def test_compare_reports_both_directions_and_nullability() -> None:
    documented = [
        SchemaColumn("core", "t", "documented_only", "bigint", False, "non-personal", "x"),
        SchemaColumn("core", "t", "both", "bigint", False, "non-personal", "x"),
    ]
    live = [
        SchemaColumn("core", "t", "both", "bigint", True),
        SchemaColumn("core", "t", "live_only", "bigint", False),
    ]

    kinds = sorted(d.kind for d in compare(documented, live, FakeExecutor({"bigint": "bigint"})))

    assert kinds == [
        "documented column does not exist",
        "nullability disagrees",
        "undocumented column",
    ]


def test_live_columns_maps_the_catalogue_rows() -> None:
    def executor(_sql: str) -> list[tuple[str, ...]]:
        return [("core", "customers", "customer_id", "bigint", "no")]

    assert live_columns(executor) == [
        SchemaColumn("core", "customers", "customer_id", "bigint", False)
    ]
