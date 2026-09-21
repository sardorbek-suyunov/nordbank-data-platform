"""Apply the numbered warehouse schema files, in order, inside one transaction.

Runs inside a container: the warehouse file is on a named volume and is not reachable from the
host by design, which is the same reason `make health` probes it from in here.

Every statement is `if not exists`, so applying twice changes nothing. The proof is the
catalogue dump this prints, which `make warehouse-apply` compares across two runs in the
acceptance evidence for criterion 1.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, "/opt/airflow/plugins")

from nordbank_ops import warehouse  # noqa: E402

SCHEMA_DIR = Path("/opt/airflow/infra/warehouse/schema")


def statements(text: str) -> list[str]:
    """Strip whole-line comments, then split on semicolons.

    Comments come out first and not second, because prose contains semicolons and splitting
    before stripping cuts a statement in half at a semicolon inside a comment. The schema files
    carry no trailing inline comment and no string literal containing a semicolon, which is
    cheaper to keep true than to carry a SQL parser for three files.
    """
    code = "\n".join(line for line in text.splitlines() if not line.strip().startswith("--"))
    return [fragment.strip() for fragment in code.split(";") if fragment.strip()]


def catalogue(connection) -> list[tuple]:
    """Every column of every table in the platform-owned schemas, in a stable order."""
    return connection.execute(
        """
        select table_schema, table_name, column_name, data_type, is_nullable
          from information_schema.columns
         where table_schema in ('ops', 'meta', 'dq')
         order by 1, 2, 3
        """
    ).fetchall()


def main() -> int:
    files = sorted(SCHEMA_DIR.glob("*.sql"))
    if not files:
        print(f"warehouse-apply: no schema files under {SCHEMA_DIR}", file=sys.stderr)
        return 1

    with warehouse.connect(read_only=False) as connection:
        warehouse.initialise(connection)
        connection.execute("begin transaction")
        applied = 0
        try:
            for path in files:
                for statement in statements(path.read_text(encoding="utf-8")):
                    connection.execute(statement)
                    applied += 1
                print(f"warehouse-apply: applied {path.name}")
            connection.execute("commit")
        except Exception:
            connection.execute("rollback")
            raise

        rows = catalogue(connection)

    tables = sorted({(row[0], row[1]) for row in rows})
    print(
        f"warehouse-apply: {len(files)} file(s), {applied} statement(s), "
        f"{len(tables)} table(s), {len(rows)} column(s)"
    )
    for schema, table in tables:
        print(f"  {schema}.{table}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
