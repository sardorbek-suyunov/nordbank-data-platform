"""Export the image's pinned requirement files from the lock (spec 007 section 2).

DuckDB is pinned once, in `pyproject.toml`'s `warehouse` group and so in `uv.lock`. The Airflow
image cannot read the lock, because its build context is `infra/docker/airflow`, so it installs
from files exported into that context:

- `requirements-warehouse.txt`: DuckDB, installed into Airflow's own environment beside the
  packages Airflow's constraints file governs;
- `requirements-dbt.txt`: the whole of dbt's environment, which is separate because dbt's
  dependencies do not resolve under Airflow's constraints.

Both carry hashes, so the image installs exactly what the lock resolved. The files are
generated and committed; `--check` regenerates them in memory and fails on any difference,
which is what stops a second, hand-edited pin from appearing. It runs in CI's `docs` job.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "infra" / "docker" / "airflow"

EXPORTS = {
    "requirements-warehouse.txt": "warehouse",
    "requirements-dbt.txt": "dbt",
}


def export(group: str) -> str:
    completed = subprocess.run(
        [
            "uv",
            "export",
            "--frozen",
            "--only-group",
            group,
            "--no-emit-project",
            "--no-header",
            "--quiet",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(f"requirements: exporting the {group} group failed:\n{completed.stderr}")
    header = (
        f"# Generated from uv.lock, the `{group}` dependency group, by "
        "scripts/requirements_export.py.\n# Do not edit: run `make requirements` after changing "
        "pyproject.toml; CI fails on any difference.\n"
    )
    return header + completed.stdout


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args(argv)

    differing = []
    for name, group in EXPORTS.items():
        wanted = export(group)
        path = TARGET / name
        if arguments.check:
            held = path.read_text(encoding="utf-8") if path.exists() else None
            if held != wanted:
                differing.append(name)
            continue
        path.write_text(wanted, encoding="utf-8", newline="\n")
        print(f"requirements: wrote {path.relative_to(ROOT).as_posix()}")

    if differing:
        print(
            "requirements: these differ from the lock: " + ", ".join(differing) + ". Run "
            "`make requirements` and commit the result; never edit them by hand."
        )
        return 1
    if arguments.check:
        print(f"requirements: {len(EXPORTS)} export(s) agree with uv.lock")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
