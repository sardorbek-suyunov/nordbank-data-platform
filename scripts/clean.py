"""Remove generated artifacts from the working tree.

Generated source data under data/ is kept: it is expensive to rebuild and removing it is a
decision the developer makes explicitly, not a side effect of clearing caches.
"""

import shutil
import sys
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRS = {".git", ".venv", "data"}
REMOVE_DIRS = {"__pycache__", ".pytest_cache", ".ruff_cache", "target", "dbt_packages", "logs"}
REMOVE_FILES = ("*.duckdb", "*.duckdb.wal", "airflow.db", "*.pyc")


def find_targets(directory: Path) -> Iterator[Path]:
    for child in sorted(directory.iterdir()):
        if child.is_dir():
            if child.name in SKIP_DIRS:
                continue
            if child.name in REMOVE_DIRS:
                yield child
            else:
                yield from find_targets(child)
        elif any(child.match(pattern) for pattern in REMOVE_FILES):
            yield child


def main() -> int:
    removed = 0
    for target in find_targets(ROOT):
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        else:
            target.unlink(missing_ok=True)
        print(f"removed {target.relative_to(ROOT).as_posix()}")
        removed += 1

    print(f"clean: removed {removed} path(s), kept data/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
