"""Lint the DuckDB SQL in the repository, or state that there is nothing to lint.

Two trees hold DuckDB SQL: the warehouse operational schema, which exists from M4, and the dbt
project, which is created in M4's later half. Both are the `duckdb` dialect `.sqlfluff`
configures. The source database's DDL under `infra/docker/postgres-source/` is deliberately not
linted here: it is PostgreSQL, and linting it against a DuckDB dialect would report differences
between two engines as style defects.

A tree with no SQL reports a skip rather than a pass, so an empty run is not mistaken for a
clean one.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TREES = (ROOT / "infra" / "warehouse", ROOT / "dbt")


def main() -> int:
    targets = []
    for tree in TREES:
        found = sorted(tree.rglob("*.sql"))
        relative = tree.relative_to(ROOT).as_posix()
        if found:
            print(f"sqlfluff: linting {len(found)} file(s) under {relative}/")
            targets.append(str(tree))
        else:
            print(f"sqlfluff skipped: no .sql files under {relative}/")

    if not targets:
        return 0
    return subprocess.call(["sqlfluff", "lint", *targets])


if __name__ == "__main__":
    sys.exit(main())
