"""Generate a data contract from the data dictionary for any entity that has none.

A one-time bootstrap, not a sync. Running it again writes nothing for an entity that already
has a contract, and says so: from the first write onward the contract is hand-authored and
version-bumped deliberately, and regenerating it would erase the lag that makes drift visible
(spec 005 section 2).

It needs a running, schema-applied stack, for one reason: the dictionary documents every
column's type, nullability and classification but not the primary key, and a contract without
a primary key cannot express the duplicate-or-missing-key rejection the ingest gate is for. The
keys are read from the live catalogue, the same place `make schema-check` reads from.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import source_db_exec as db  # noqa: E402
from data_contract import (  # noqa: E402
    SOURCE_SYSTEM,
    Contract,
    ContractColumn,
    dictionary_revision,
    dump,
    load_all,
)
from schema_contract import read_dictionary  # noqa: E402

DICTIONARY = ROOT / "docs" / "data_dictionary.md"
CONTRACTS = ROOT / "contracts" / SOURCE_SYSTEM

# `platform` is the simulation's own bookkeeping, not the bank's data. The extractor reads
# `platform.column_classifications` to decide what to tokenise and reads `platform.tick_log`
# for the source's control total; it lands neither, and neither gets a contract.
EXTRACTED_SCHEMAS: tuple[str, ...] = ("core", "ref")

WATERMARK_COLUMN = "updated_at"

PRIMARY_KEY_SQL = """
select tc.table_schema, tc.table_name, k.column_name
  from information_schema.table_constraints tc
  join information_schema.key_column_usage k
    on k.constraint_name = tc.constraint_name
   and k.table_schema = tc.table_schema
 where tc.constraint_type = 'PRIMARY KEY'
   and tc.table_schema in ('core', 'ref')
 order by 1, 2, k.ordinal_position
"""


def primary_keys(execute) -> dict[tuple[str, str], list[str]]:
    keys: dict[tuple[str, str], list[str]] = {}
    for schema, table, column in execute(PRIMARY_KEY_SQL):
        keys.setdefault((schema, table), []).append(column)
    return keys


def build(schema: str, table: str, columns, revision: str, primary_key: str) -> Contract:
    return Contract(
        source_system=SOURCE_SYSTEM,
        source_schema=schema,
        entity=table,
        contract_version=1,
        watermark_column=WATERMARK_COLUMN,
        primary_key=primary_key,
        dictionary_revision=revision,
        columns=tuple(
            ContractColumn(
                name=column.name,
                data_type=column.data_type,
                is_nullable=column.is_nullable,
                classification=column.classification,
            )
            for column in columns
        ),
    )


def main() -> int:
    db.require_stack()
    execute = db.executor()

    documented = [c for c in read_dictionary(DICTIONARY) if c.schema in EXTRACTED_SCHEMAS]
    by_entity: dict[tuple[str, str], list] = {}
    for column in documented:
        by_entity.setdefault((column.schema, column.table), []).append(column)

    keys = primary_keys(execute)
    revision = dictionary_revision(DICTIONARY)
    CONTRACTS.mkdir(parents=True, exist_ok=True)
    existing = load_all(CONTRACTS)

    written, skipped, refused = [], [], []
    for (schema, table), columns in sorted(by_entity.items()):
        if table in existing:
            held = existing[table]
            if held.source_schema != schema:
                refused.append(
                    f"{schema}.{table}: an entity named {table!r} is already contracted "
                    f"from {held.source_schema}"
                )
            else:
                skipped.append(f"{schema}.{table}")
            continue

        key_columns = keys.get((schema, table), [])
        if len(key_columns) != 1:
            refused.append(
                f"{schema}.{table}: expected exactly one primary key column, "
                f"found {len(key_columns)}"
            )
            continue
        if not any(column.name == WATERMARK_COLUMN for column in columns):
            refused.append(f"{schema}.{table}: no {WATERMARK_COLUMN} column to watermark on")
            continue

        contract = build(schema, table, columns, revision, key_columns[0])
        # newline="" leaves the rendered text alone, so the file is LF on every platform and
        # matches what `.gitattributes` checks out.
        (CONTRACTS / f"{table}.yml").write_text(dump(contract), encoding="utf-8", newline="")
        written.append(f"{schema}.{table}")

    for entity in written:
        print(f"contracts-bootstrap: wrote {entity}")
    if skipped:
        print(f"contracts-bootstrap: {len(skipped)} entity(ies) already contracted, left alone")
    for problem in refused:
        print(f"contracts-bootstrap: refused {problem}", file=sys.stderr)

    print(
        f"contracts-bootstrap: {len(written)} written, {len(skipped)} skipped, "
        f"{len(refused)} refused, dictionary at {revision}"
    )
    return 1 if refused else 0


if __name__ == "__main__":
    raise SystemExit(main())
