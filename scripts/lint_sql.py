"""Lint the DuckDB SQL in the repository, or state that there is nothing to lint.

Two trees hold DuckDB SQL: the warehouse operational schema, which exists from M4, and the dbt
project, which is created in M4's later half. Both are the `duckdb` dialect `.sqlfluff`
configures. The source database's DDL under `infra/docker/postgres-source/` is deliberately not
linted here: it is PostgreSQL, and linting it against a DuckDB dialect would report differences
between two engines as style defects.

A tree with no SQL reports a skip rather than a pass, so an empty run is not mistaken for a
clean one.

Two parts of the dbt tree are not linted, and each has a check of its own instead (spec 007):

- the bronze models are generated from the contracts, one call to a macro each, and
  `make dbt-generate CHECK=1` is their check;
- the macros are Jinja programs, and the SQL they render is built and tested by
  `dbt build --warn-error` in the stack and by `make dbt-prove` on planted fixtures.

The dbt templater, which would lint the rendered SQL, compiles against a live warehouse and lake:
the read macro asks the batch registry at compile time which entities have landed rows, and
this job has neither. Hand-written dbt SQL, which starts with silver, is linted.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TREES = (ROOT / "infra" / "warehouse", ROOT / "dbt")
NOT_LINTED = (ROOT / "dbt" / "models" / "bronze", ROOT / "dbt" / "macros")


def main() -> int:
    targets = []
    for tree in TREES:
        found = sorted(
            path
            for path in tree.rglob("*.sql")
            if not any(path.is_relative_to(excluded) for excluded in NOT_LINTED)
        )
        relative = tree.relative_to(ROOT).as_posix()
        if found:
            print(f"sqlfluff: linting {len(found)} file(s) under {relative}/")
            targets.extend(str(path) for path in found)
        else:
            print(f"sqlfluff skipped: no hand-written .sql files under {relative}/")

    if not targets:
        return 0
    # sqlfluff ends a run on a terminal with an emoji, and on Windows `isatty()` is true for the
    # null device, so `make lint >/dev/null` takes that path while Python encodes the output in the
    # ANSI code page, which has no emoji: a clean lint then crashed on its own summary and exited 1.
    # UTF-8 output makes the summary writable wherever it goes; nothing else about the run changes.
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    return subprocess.call(["sqlfluff", "lint", *targets], env=env)


if __name__ == "__main__":
    sys.exit(main())
