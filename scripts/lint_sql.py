"""Lint the SQL in the dbt project, or state that there is nothing to lint.

The dbt project is created at M4. Until then the tree holds no SQL, and this reports a skip
instead of a pass so that an empty run is not mistaken for a clean run.
"""

import subprocess
import sys
from pathlib import Path

DBT_DIR = Path(__file__).resolve().parent.parent / "dbt"


def main() -> int:
    sql_files = sorted(DBT_DIR.rglob("*.sql"))
    if not sql_files:
        print("sqlfluff skipped: no .sql files under dbt/ (the dbt project arrives at M4)")
        return 0

    print(f"sqlfluff: linting {len(sql_files)} file(s) under dbt/")
    return subprocess.call(["sqlfluff", "lint", str(DBT_DIR)])


if __name__ == "__main__":
    sys.exit(main())
