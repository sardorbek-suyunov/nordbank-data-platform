"""Apply the source schema, the reference seeds and the column classifications.

Three phases, in order, each idempotent:

1. every file under `infra/docker/postgres-source/schema/`, numbered and applied in order;
2. every file under `infra/docker/postgres-source/seed/`, likewise;
3. `platform.column_classifications`, generated from `docs/data_dictionary.md`.

Phase 3 is why this is a script rather than `psql -f` in a loop. The dictionary is the single
source of truth for the classification (spec 002 section 5), so the table is generated from it
and there is no hand-written seed file to drift. The cost is that applying the schema is not
purely "run the files in order", and that is stated in ADR 0009 rather than hidden.

The dictionary is parsed before anything is written. A malformed dictionary aborts the apply
with the offending line, and no partial classification is ever loaded.

**Drift events that have already fired are re-applied**, and the columns they added are
classified along with the documented ones. Phase 3 reloads the classification table from the
dictionary and would otherwise drop the classification a drift event added, turning
`schema-check` red; refusing to run while `platform.drift_log` is non-empty would make this
target unusable after any tick. Re-applying is deterministic from the log, and every event's DDL
is written to be idempotent like every other file here (spec 004 section 3, as amended).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import source_db_exec as db  # noqa: E402
from schema_contract import DictionaryError, read_dictionary  # noqa: E402

sys.path.insert(0, str(ROOT))
from generator import drift  # noqa: E402

SCHEMA_DIR = ROOT / "infra" / "docker" / "postgres-source" / "schema"
SEED_DIR = ROOT / "infra" / "docker" / "postgres-source" / "seed"
DICTIONARY = ROOT / "docs" / "data_dictionary.md"


def _apply_directory(directory: Path, container_root: str) -> int:
    files = sorted(directory.glob("*.sql"))
    if not files:
        raise SystemExit(f"schema-apply: no SQL files under {directory}")
    for path in files:
        print(f"schema-apply: {path.name}")
        db.run_sql_file(f"{container_root}/{path.name}")
    return len(files)


def _classification_sql(columns: list) -> str:
    """Upsert every documented classification, then remove any that is no longer documented.

    Upsert rather than delete-and-insert so that a re-run with an unchanged dictionary writes
    nothing at all: the `where` clause keeps updated_at still, which is what makes acceptance
    criterion 1 true for this table as well as for the seeds.
    """
    rows = ",\n    ".join(
        f"({db.quote_literal(column.schema)}, "
        f"{db.quote_literal(column.table)}, "
        f"{db.quote_literal(column.name)}, "
        f"{db.quote_literal(column.classification)}, "
        f"{db.quote_literal(column.description)})"
        for column in columns
    )
    return f"""
begin;

create temporary table documented_classifications (
    schema_name    varchar(63),
    table_name     varchar(63),
    column_name    varchar(63),
    classification varchar(20),
    rationale      text
) on commit drop;

insert into documented_classifications values
    {rows};

insert into platform.column_classifications
    (schema_name, table_name, column_name, classification, rationale)
select schema_name, table_name, column_name, classification, rationale
  from documented_classifications
on conflict (schema_name, table_name, column_name) do update
    set classification = excluded.classification,
        rationale      = excluded.rationale
  where (platform.column_classifications.classification,
         platform.column_classifications.rationale)
        is distinct from (excluded.classification, excluded.rationale);

delete from platform.column_classifications c
 where not exists (
     select 1
       from documented_classifications d
      where d.schema_name = c.schema_name
        and d.table_name  = c.table_name
        and d.column_name = c.column_name
 );

commit;
"""


def main() -> int:
    db.require_stack()

    try:
        columns = read_dictionary(DICTIONARY)
    except DictionaryError as error:
        print(f"schema-apply: {error}", file=sys.stderr)
        print("schema-apply: nothing was applied", file=sys.stderr)
        return 1

    schema_files = _apply_directory(SCHEMA_DIR, "/schema")
    seed_files = _apply_directory(SEED_DIR, "/seed")

    fired = drift.fired_names(db.executor())
    for name in fired:
        event = drift.event_by_name(name)
        if event is None:
            continue
        print(f"schema-apply: re-applying drift event {name}")
        db.run_sql(event.apply_sql + ";\n")
    columns = columns + drift.classification_rows(fired)

    print(f"schema-apply: classifying {len(columns)} columns from {DICTIONARY.name}")
    db.run_sql(_classification_sql(columns))

    print(
        f"schema-apply: {schema_files} schema file(s), {seed_files} seed file(s), "
        f"{len(columns)} classification(s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
