"""Fail when the live schema and the committed data dictionary disagree.

This is the control that stops the dictionary rotting (spec 002 section 7). It checks four
things, in both directions:

- no column exists in the database that the dictionary does not document;
- no column is documented that the database does not have;
- types agree, after resolving Postgres aliases through the database itself;
- nullability agrees.

It also checks that `platform.column_classifications` matches the dictionary, because the table
is generated from it and a stale table means the extraction layer at M4 would tokenise the
wrong set of columns.

Wired into `make test-integration`, and run directly by `make schema-check`.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import source_db_exec as db  # noqa: E402
from schema_contract import (  # noqa: E402
    DictionaryError,
    Difference,
    compare,
    live_columns,
    read_dictionary,
)

DICTIONARY = ROOT / "docs" / "data_dictionary.md"


def classification_differences(execute, documented) -> list[Difference]:
    """Both directions of acceptance criterion 6, against the generated table."""
    rows = execute(
        """
        select schema_name, table_name, column_name, classification
          from platform.column_classifications
         order by 1, 2, 3
        """
    )
    stored = {(row[0], row[1], row[2]): row[3] for row in rows}
    expected = {(c.schema, c.table, c.name): c.classification for c in documented}

    differences: list[Difference] = []
    for key in sorted(stored.keys() - expected.keys()):
        differences.append(
            Difference("classified column is not documented", ".".join(key), stored[key])
        )
    for key in sorted(expected.keys() - stored.keys()):
        differences.append(Difference("documented column is not classified", ".".join(key), ""))
    for key in sorted(stored.keys() & expected.keys()):
        if stored[key] != expected[key]:
            differences.append(
                Difference(
                    "classification disagrees",
                    ".".join(key),
                    f"dictionary says {expected[key]}, database says {stored[key]}",
                )
            )
    return differences


def main() -> int:
    db.require_stack()

    try:
        documented = read_dictionary(DICTIONARY)
    except DictionaryError as error:
        print(f"schema-check: {error}", file=sys.stderr)
        return 1

    execute = db.executor()
    differences = compare(documented, live_columns(execute), execute)
    differences += classification_differences(execute, documented)

    if differences:
        for difference in differences:
            print(f"schema-check: {difference}")
        print(
            f"schema-check: {len(differences)} difference(s) between the database "
            f"and {DICTIONARY.name}"
        )
        return 1

    print(f"schema-check: {len(documented)} columns agree with {DICTIONARY.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
