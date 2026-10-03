"""Prove every bronze test and rule can fail, on planted fixtures (spec 007 criteria 3, 4, 7, 8).

A test that has never failed has not been shown to test anything. This builds a scratch dbt
project in a temporary directory: the repository's own macros, copied byte for byte, a lake of
local Parquet objects written the way the platform writes them, and a scratch registry. Each
fixture entity plants one violation, and the run requires:

- every generic test to pass on the clean entity and to fail on the entity planted for it;
- the registered-batch filter to keep out an object under an unregistered batch id and a copy
  of a registered file under a new key;
- an entity with no registered rows to build as an empty relation of the declared types;
- `bronze_guard` to fail the build on a bronze model that reads the lake itself, on a model
  outside bronze that does, and on a test that stores its failures;
- every macro test under `dbt/tests/macros` to pass on the repository's macros, and to fail on a
  copy of the macros with one planted defect (specification 008), each defect named in
  `MUTATIONS` with the test that must catch it.

It needs dbt and DuckDB and nothing else: no stack, no network. It runs from a temporary
directory because dbt 1.12 loads the first `.env` it finds above its working directory, and the
repository's holds every secret the stack uses.

Usage: `uv run --group dbt python scripts/dbt_prove.py`, which `make dbt-prove` runs.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "airflow" / "plugins"))

TOKEN = "0123456789abcdef0123456789abcdef"
# A key of `project()`'s extra files that appends to the project file rather than adding a file.
PROJECT_SUFFIX = "dbt_project.yml+"
OPENED = dt.datetime(2026, 7, 20, 4, 0, tzinfo=dt.UTC)

# Each fixture entity, the rows of its one registered batch, and the test planted to fail on it.
# A row is (key, name, source_file). `name` is the identifier column.
CLEAN = [(1, TOKEN, "core.fixture"), (2, None, "core.fixture")]
CASES: dict[str, dict] = {
    "clean": {"rows": CLEAN, "fails": None},
    "duplicate_key": {"rows": [(1, TOKEN, "core.fixture")] * 2, "fails": "bronze_unique_key"},
    "null_key": {"rows": [(None, TOKEN, "core.fixture")], "fails": "grain_not_null"},
    "null_audit": {"rows": [(1, TOKEN, None)], "fails": "audit_not_null"},
    "reconcile": {"rows": CLEAN, "landed": 3, "fails": "bronze_landing_reconciliation"},
    "token": {"rows": [(1, "Ann Example", "core.fixture")], "fails": "bronze_token"},
    "hive_batch": {"rows": CLEAN, "file_batch": "another-batch-01", "fails": "bronze_hive_key"},
    "hive_date": {"rows": CLEAN, "registry_date": "2026-07-21", "fails": "bronze_hive_key"},
    "planted": {"rows": CLEAN, "plant": True, "fails": None},
    "empty": {"rows": [], "fails": None},
}
TESTS = ("bronze_unique_key", "grain_not_null", "audit_not_null", "bronze_landing_reconciliation",
         "bronze_token", "bronze_hive_key")  # fmt: skip


def batch_id(entity: str, sequence: int = 1) -> str:
    return f"{entity}-20260720T000000-{sequence:02d}"


def write_object(lake: Path, entity: str, key_batch: str, day: str, rows, file_batch: str):
    import pyarrow as pa
    import pyarrow.parquet as pq

    folder = lake / "bronze" / "fixture" / entity / f"ingest_date={day}" / f"batch_id={key_batch}"
    folder.mkdir(parents=True, exist_ok=True)
    schema = pa.schema(
        [
            ("fixture_id", pa.int64()),
            ("name", pa.string()),
            ("_ingested_at", pa.timestamp("us", tz="UTC")),
            ("_source_file", pa.string()),
            ("_batch_id", pa.string()),
            ("_source_system", pa.string()),
        ]
    )
    table = pa.table(
        {
            "fixture_id": [r[0] for r in rows],
            "name": [r[1] for r in rows],
            "_ingested_at": [OPENED] * len(rows),
            "_source_file": [r[2] for r in rows],
            "_batch_id": [file_batch] * len(rows),
            "_source_system": ["fixture"] * len(rows),
        },
        schema=schema,
    )
    pq.write_table(table, folder / "part-0000.parquet", compression="snappy")


def build_fixtures(work: Path) -> Path:
    import duckdb

    lake = work / "lake"
    database = work / "prove.duckdb"
    connection = duckdb.connect(str(database))
    connection.execute("create schema ops")
    connection.execute(
        """
        create table ops.batch_registry (
            batch_id varchar, source_system varchar, entity varchar, ingest_date date,
            rows_landed bigint, status varchar
        )
        """
    )
    for entity, case in CASES.items():
        registered = batch_id(entity)
        rows = case["rows"]
        if rows:
            write_object(
                lake, entity, registered, "2026-07-20", rows, case.get("file_batch", registered)
            )
        connection.execute(
            "insert into ops.batch_registry values (?, 'fixture', ?, ?, ?, 'registered')",
            [registered, entity, case.get("registry_date", "2026-07-20"),
             case.get("landed", len(rows))],
        )  # fmt: skip
        if case.get("plant"):
            # An object under a batch id the registry never registered, as a run that died after
            # writing leaves; and a copy of the registered file under a new key.
            write_object(lake, entity, batch_id(entity, 2), "2026-07-21", rows, batch_id(entity, 2))
            write_object(lake, entity, batch_id(entity, 3), "2026-07-22", rows, registered)
            connection.execute(
                "insert into ops.batch_registry "
                "values (?, 'fixture', ?, '2026-07-21', 2, 'failed')",
                [batch_id(entity, 2), entity],
            )
    # A registered batch that landed nothing writes no object, and the entity with only that is
    # the empty case.
    connection.close()
    return database


def project(work: Path, database: Path, extra_models: dict[str, str] | None = None) -> Path:
    home = work / "project"
    if home.exists():
        shutil.rmtree(home)
    (home / "models" / "bronze").mkdir(parents=True)
    shutil.copytree(ROOT / "dbt" / "macros", home / "macros")
    shutil.copy(ROOT / "dbt" / "models" / "bronze" / "_sources.yml", home / "models" / "bronze")
    (home / "dbt_project.yml").write_text(
        "name: prove\nversion: '1.0'\nconfig-version: 2\nprofile: prove\n"
        "flags:\n  send_anonymous_usage_stats: false\n"
        'on-run-start:\n  - "{{ bronze_guard() }}"\n'
        "models:\n  prove:\n    bronze:\n      +materialized: view\n      +schema: bronze\n",
        encoding="utf-8",
    )
    (home / "profiles.yml").write_text(
        f"prove:\n  target: prove\n  outputs:\n    prove:\n      type: duckdb\n"
        f"      path: '{database.as_posix()}'\n      schema: main\n      threads: 1\n",
        encoding="utf-8",
    )
    properties = ["version: 2", "models:"]
    for entity in CASES:
        name = f"br_fixture__{entity}"
        (home / "models" / "bronze" / f"{name}.sql").write_text(
            f"{{{{ bronze_read('fixture', '{entity}') }}}}\n", encoding="utf-8"
        )
        properties += [
            f"  - name: {name}",
            "    data_tests:",
            "      - bronze_unique_key:",
            "          arguments: {columns: [fixture_id, _batch_id]}",
            "      - bronze_not_null:",
            f"          name: {name}_grain_not_null",
            "          arguments: {columns: [fixture_id, _batch_id]}",
            "      - bronze_not_null:",
            f"          name: {name}_audit_not_null",
            "          arguments:",
            "            columns: [_ingested_at, _source_file, _batch_id, _source_system]",
            "      - bronze_landing_reconciliation:",
            f"          arguments: {{source_system: fixture, entity: {entity}}}",
            "      - bronze_hive_key",
            "    columns:",
            "      - {name: fixture_id, data_type: BIGINT}",
            "      - name: name",
            "        data_type: VARCHAR",
            "        data_tests:",
            "          - bronze_token: {arguments: {width: 32}}",
            "      - {name: _ingested_at, data_type: TIMESTAMP WITH TIME ZONE}",
            "      - {name: _source_file, data_type: VARCHAR}",
            "      - {name: _batch_id, data_type: VARCHAR}",
            "      - {name: _source_system, data_type: VARCHAR}",
            "      - {name: _object_batch_id, data_type: VARCHAR}",
            "      - {name: _object_ingest_date, data_type: DATE}",
        ]
    (home / "models" / "bronze" / "fixtures.yml").write_text(
        "\n".join(properties) + "\n", encoding="utf-8"
    )
    for relative, text in (extra_models or {}).items():
        if relative == PROJECT_SUFFIX:
            with (home / "dbt_project.yml").open("a", encoding="utf-8") as handle:
                handle.write(text)
            continue
        path = home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return home


def dbt(home: Path, work: Path, lake: Path, *arguments: str) -> subprocess.CompletedProcess:
    executable = Path(sys.executable).parent / ("dbt.exe" if os.name == "nt" else "dbt")
    env = {
        **os.environ,
        "DBT_PROFILES_DIR": str(home),
        "DBT_TARGET_PATH": str(work / "target"),
        "DBT_LOG_PATH": str(work / "logs"),
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
    }
    command = [str(executable), *arguments, "--project-dir", str(home),
               "--vars", json.dumps({"lake_root": lake.as_posix()})]  # fmt: skip
    return subprocess.run(command, cwd=work, env=env, capture_output=True, text=True, check=False)


def outcomes(work: Path) -> dict[tuple[str, str], str]:
    """(model, test) -> status, from the run's results and manifest."""
    manifest = json.loads((work / "target" / "manifest.json").read_text(encoding="utf-8"))
    results = json.loads((work / "target" / "run_results.json").read_text(encoding="utf-8"))
    out = {}
    for result in results["results"]:
        node = manifest["nodes"].get(result["unique_id"], {})
        if node.get("resource_type") != "test":
            continue
        model = node["depends_on"]["nodes"][-1].split(".")[-1]
        name = node["test_metadata"]["name"]
        if name == "bronze_not_null":
            name = (
                "grain_not_null" if node["name"].endswith("_grain_not_null") else "audit_not_null"
            )
        out[(model.removeprefix("br_fixture__"), name)] = result["status"]
    return out


def main() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="dbt-prove-") as scratch:
        work = Path(scratch)
        database = build_fixtures(work)
        lake = work / "lake"

        home = project(work, database)
        run = dbt(home, work, lake, "build")
        results = outcomes(work)
        print(
            f"dbt-prove: fixture build exited {run.returncode} with {len(results)} test result(s)"
        )
        if len(results) < len(CASES) * len(TESTS) - 1:
            failures.append(f"only {len(results)} test results; the fixtures did not build")
        for entity, case in CASES.items():
            for test in TESTS:
                status = results.get((entity, test))
                expected = "fail" if case["fails"] == test else "pass"
                if status != expected:
                    failures.append(f"{entity}: {test} was {status}, expected {expected}")
            planted = case["fails"]
            if planted:
                print(f"dbt-prove: {planted} fails on the planted {entity} fixture")

        import duckdb

        connection = duckdb.connect(str(database), read_only=True)
        held = connection.execute(
            "select count(*), count(distinct _object_batch_id) from bronze.br_fixture__planted"
        ).fetchone()
        if held != (2, 1):
            failures.append(f"planted objects reached the model: {held} rows and batches")
        else:
            print(
                "dbt-prove: an object under an unregistered batch id and a copy of a registered "
                "file under a new key are both absent from the model"
            )
        empty = connection.execute("select count(*) from bronze.br_fixture__empty").fetchone()[0]
        types = dict(
            connection.execute(
                "select column_name, data_type from information_schema.columns "
                "where table_schema = 'bronze' and table_name = 'br_fixture__empty'"
            ).fetchall()
        )
        connection.close()
        if empty != 0 or types.get("_ingested_at") != "TIMESTAMP WITH TIME ZONE" or len(types) < 8:
            failures.append(f"the empty entity built as {empty} rows with {types}")
        else:
            print(
                f"dbt-prove: the entity with no landed rows is empty, typed, {len(types)} columns"
            )

        guard_cases = {
            "a bronze model reading the lake itself": {
                "models/bronze/br_fixture__rogue.sql":
                    "select * from read_parquet('lake/bronze/fixture/clean/*/*/*.parquet')\n",
            },
            "a model outside bronze reading the lake": {
                "models/silver/sl_rogue.sql": "select * from read_parquet('s3://lake/x.parquet')\n",
            },
            "a test storing its failures": {
                PROJECT_SUFFIX: "data_tests:\n  +store_failures: true\n",
            },
        }  # fmt: skip
        for label, extra in guard_cases.items():
            home = project(work, database, extra)
            run = dbt(home, work, lake, "build")
            refused = run.returncode != 0 and "bronze_guard" in run.stdout
            if refused:
                print(f"dbt-prove: bronze_guard fails the build on {label}")
            else:
                failures.append(f"bronze_guard did not fail the build on {label}")

        failures += prove_macro_tests(work)

    if failures:
        print("\ndbt-prove: FAILED")
        for line in failures:
            print(f"  {line}")
        return 1
    print("\ndbt-prove: every test and rule failed where it was planted, and passed where not")
    return 0


# --- macro tests (specification 008) ---------------------------------------------------------

MACRO_TESTS = ROOT / "dbt" / "tests" / "macros"

# Each planted defect: what it is, the macro file, the text replaced, its replacement, and the
# test that must then fail. A text that does not occur exactly once fails the proof, so a
# refactored macro cannot quietly turn a defect into a no-op.
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    (
        "the conversion by decimal division",
        "fx.sql",
        "{{ fx_units_to_eur(fx_quotient_units(amount, rate)) }}",
        "round(cast({{ amount }} as decimal(18,4)) / cast({{ rate }} as decimal(18,8)), 4)",
        "assert_fx_conversion_is_exact",
    ),
    (
        "the scaling back by division",
        "fx.sql",
        "(cast({{ units }} as decimal(38,0)) * cast(0.0001 as decimal(38,4)))",
        "(cast({{ units }} as decimal(38,0)) / 10000)",
        "assert_fx_conversion_is_exact",
    ),
    (
        "the rate scaled without widening",
        "fx.sql",
        "cast(cast({{ rate }} as decimal(38,8)) * 100000000 as hugeint)",
        "cast(cast({{ rate }} as decimal(18,8)) * 100000000 as hugeint)",
        "assert_fx_conversion_is_exact",
    ),
    (
        "the quotient truncated rather than rounded",
        "fx.sql",
        "cast(100000000 as hugeint) + {{ fx_rate_units(rate) }})",
        "cast(100000000 as hugeint))",
        "assert_fx_conversion_is_exact",
    ),
    (
        "the publication instant in UTC",
        "fx.sql",
        "timezone('Europe/Berlin',",
        "timezone('UTC',",
        "assert_fx_publication_instant",
    ),
]
# A floor stated as a number, never read from the folder: the macro tests that exist.
MACRO_TEST_FLOOR = 2


def macro_project(work: Path, mutation: tuple[str, str, str] | None = None) -> Path:
    """A project of the repository's macros and macro tests, with at most one planted defect."""
    home = work / "macro-project"
    if home.exists():
        shutil.rmtree(home)
    shutil.copytree(ROOT / "dbt" / "macros", home / "macros")
    shutil.copytree(MACRO_TESTS, home / "tests" / "macros")
    if mutation is not None:
        file, old, new = mutation
        path = home / "macros" / file
        text = path.read_text(encoding="utf-8")
        if text.count(old) != 1:
            raise SystemExit(
                f"dbt-prove: a planted defect's text occurs {text.count(old)} time(s) in {file}, "
                "not once; update MUTATIONS"
            )
        path.write_text(text.replace(old, new), encoding="utf-8")
    (home / "dbt_project.yml").write_text(
        "name: prove_macros\nversion: '1.0'\nconfig-version: 2\nprofile: prove\n"
        "flags:\n  send_anonymous_usage_stats: false\n",
        encoding="utf-8",
    )
    database = work / "macros.duckdb"
    (home / "profiles.yml").write_text(
        f"prove:\n  target: prove\n  outputs:\n    prove:\n      type: duckdb\n"
        f"      path: '{database.as_posix()}'\n      schema: main\n      threads: 1\n",
        encoding="utf-8",
    )
    return home


def test_statuses(work: Path) -> dict[str, str]:
    """Singular test name -> status, from the last run's results."""
    results = json.loads((work / "target" / "run_results.json").read_text(encoding="utf-8"))
    return {r["unique_id"].split(".")[2]: r["status"] for r in results["results"]}


def prove_macro_tests(work: Path) -> list[str]:
    """Every macro test passes on the macros, and fails on each planted defect."""
    names = sorted(p.stem for p in MACRO_TESTS.glob("*.sql"))
    if len(names) < MACRO_TEST_FLOOR:
        return [f"only {len(names)} macro test(s) under dbt/tests/macros"]
    failures = []
    dbt(macro_project(work), work, work / "lake", "test")
    statuses = test_statuses(work)
    for name in names:
        if statuses.get(name) != "pass":
            failures.append(f"{name} was {statuses.get(name)} on the repository's macros")
    if not failures:
        print(f"dbt-prove: {len(names)} macro tests pass on the repository's macros")
    for label, file, old, new, test in MUTATIONS:
        dbt(macro_project(work, (file, old, new)), work, work / "lake", "test", "--select", test)
        status = test_statuses(work).get(test)
        if status in ("fail", "error"):
            note = " (an error)" if status == "error" else ""
            print(f"dbt-prove: {test} fails on {label}{note}")
        else:
            failures.append(f"{test} was {status} on {label}, expected it to fail")
    return failures


if __name__ == "__main__":
    raise SystemExit(main())
