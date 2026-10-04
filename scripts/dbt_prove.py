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
- `gold_guard` to fail the build on a gold model selecting an undeclared quasi-identifier, under
  its own name or a new one, on a gold column with no classification and on a model that does not
  enforce its contract, and to pass one selecting a declared quasi-identifier or a generalisation;
- every macro test under `dbt/tests/macros` to pass on the repository's macros, and to fail on a
  copy of the macros with one planted defect (specification 008), each defect named in
  `MUTATIONS` with the test that must catch it;
- every generic silver test to pass on a clean fixture model and to fail on the fixture planted
  for it (`SILVER_FIXTURES`).

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
        'on-run-start:\n  - "{{ bronze_guard() }}"\n  - "{{ gold_guard() }}"\n'
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

        for label, (gold_sql, gold_columns, enforced, refuse) in GOLD_CASES.items():
            extra = gold_fixture(gold_sql, gold_columns, enforced)
            home = project(work, database, extra)
            run = dbt(home, work, lake, "build", "--select", "path:models/silver path:models/gold")
            refused = "gold_guard" in run.stdout
            if not refuse and run.returncode != 0:
                failures.append(f"the build of {label} exited {run.returncode}")
            elif refused == refuse:
                verdict = "fails the build on" if refuse else "passes"
                print(f"dbt-prove: gold_guard {verdict} {label}")
            else:
                failures.append(f"gold_guard was {'refusing' if refused else 'silent'} on {label}")

        failures += prove_macro_tests(work)
        failures += prove_silver_tests(work)

    if failures:
        print("\ndbt-prove: FAILED")
        for line in failures:
            print(f"  {line}")
        return 1
    print("\ndbt-prove: every test and rule failed where it was planted, and passed where not")
    return 0


# --- the gold column guard (specification 008 section 8) -----------------------------------

# A silver model whose properties classify its columns, as the generated ones do, one of them a
# quasi-identifier the declaration file permits, and gold models over it: (gold SQL, the columns
# its properties declare as (name, classification or None), whether its contract is enforced, and
# whether the guard must refuse it).
GOLD_CASES: dict[str, tuple[str, list, bool, bool]] = {
    "a gold model selecting only the band": (
        "select customer_id, age_band from {{ ref('sl_people') }}",
        [("customer_id", "pseudonymous_key"), ("age_band", "non-personal")], True, False,
    ),
    "a gold model selecting a declared quasi-identifier": (
        "select customer_id, country_code from {{ ref('sl_people') }}",
        [("customer_id", "pseudonymous_key"), ("country_code", "quasi-identifier")], True, False,
    ),
    "a gold model selecting an undeclared quasi-identifier": (
        "select customer_id, date_of_birth from {{ ref('sl_people') }}",
        [("customer_id", "pseudonymous_key"), ("date_of_birth", "quasi-identifier")], True, True,
    ),
    "a gold model selecting it as non-personal": (
        "select customer_id, date_of_birth from {{ ref('sl_people') }}",
        [("customer_id", "pseudonymous_key"), ("date_of_birth", "non-personal")], True, True,
    ),
    "a gold model renaming it, classified as what it is": (
        "select customer_id, date_of_birth as born from {{ ref('sl_people') }}",
        [("customer_id", "pseudonymous_key"), ("born", "quasi-identifier")], True, True,
    ),
    "a gold column with no classification": (
        "select customer_id, age_band from {{ ref('sl_people') }}",
        [("customer_id", "pseudonymous_key"), ("age_band", None)], True, True,
    ),
    "a gold model that does not enforce its contract": (
        "select customer_id, age_band from {{ ref('sl_people') }}",
        [("customer_id", "pseudonymous_key"), ("age_band", "non-personal")], False, True,
    ),
}  # fmt: skip
PEOPLE = {
    "customer_id": ("BIGINT", "pseudonymous_key", None),
    "date_of_birth": ("DATE", "quasi-identifier", None),
    "country_code": ("VARCHAR", "quasi-identifier", "identity: the published granularity"),
    "age_band": ("VARCHAR", "non-personal", None),
}
TYPES = {"born": "DATE", **{name: spec[0] for name, spec in PEOPLE.items()}}


def gold_fixture(gold_sql: str, gold_columns: list, enforced: bool) -> dict[str, str]:
    silver = ["version: 2", "models:", "  - name: sl_people", "    columns:"]
    for name, (data_type, classification, generalisation) in PEOPLE.items():
        meta = f"classification: {classification}"
        if generalisation:
            meta += f', gold_generalisation: "{generalisation}"'
        silver += [
            f"      - name: {name}",
            f"        data_type: {data_type}",
            f"        config: {{meta: {{{meta}}}}}",
        ]
    gold = ["version: 2", "models:", "  - name: dim_people"]
    gold += ["    config:", f"      contract: {{enforced: {'true' if enforced else 'false'}}}"]
    gold += ["    columns:"]
    for name, classification in gold_columns:
        gold += [f"      - name: {name}", f"        data_type: {TYPES[name]}"]
        if classification:
            gold.append(f"        config: {{meta: {{classification: {classification}}}}}")
    return {
        "models/silver/sl_people.sql": (
            "select cast(1 as bigint) as customer_id, date '1990-05-01' as date_of_birth, "
            "'FR' as country_code, '25-34' as age_band\n"
        ),
        "models/silver/sl_people.yml": "\n".join(silver) + "\n",
        "models/gold/dim_people.sql": gold_sql + "\n",
        "models/gold/dim_people.yml": "\n".join(gold) + "\n",
    }


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
    (
        "a projection compared with <> rather than is distinct from",
        "historisation.sql",
        '({%- for column in projection %}\n            "{{ column }}" is distinct from lag(',
        '({%- for column in projection %}\n            "{{ column }}" <> lag(',
        "assert_scd2_version_rule",
    ),
    (
        "a change the contract does not describe opening no version",
        "historisation.sql",
        "or (not _deleted and (_projection_changed or not _excluded_changed))",
        "or (not _deleted and _projection_changed)",
        "assert_scd2_version_rule",
    ),
    (
        "the latest batch of a re-read winning",
        "historisation.sql",
        "order by {{ key | join(', ') }}, updated_at, _batch_id",
        "order by {{ key | join(', ') }}, updated_at, _batch_id desc",
        "assert_scd2_version_rule",
    ),
    (
        "the first version opening at its updated_at",
        "historisation.sql",
        "then {{ scd2_epoch() }} else updated_at end",
        "then updated_at else updated_at end",
        "assert_scd2_version_rule",
    ),
    (
        "a deletion that does not close the version before it",
        "historisation.sql",
        "or _deleted is distinct from _previous_deleted",
        "or (not _deleted and _previous_deleted)",
        "assert_scd2_version_rule",
    ),
    (
        "is_active treated as an audit column",
        "historisation.sql",
        "'_object_ingest_date', '_raw_payload']",
        "'_object_ingest_date', '_raw_payload', 'is_active']",
        "assert_scd2_inactive_ref_is_current",
    ),
    (
        "latest state keeping the oldest observation",
        "historisation.sql",
        "order by {{ key | join(', ') }}, updated_at desc",
        "order by {{ key | join(', ') }}, updated_at",
        "assert_latest_state",
    ),
    (
        "business validity ending on the inclusive last day",
        "historisation.sql",
        "coalesce({{ valid_to_inclusive }} + 1, date '9999-12-31')",
        "coalesce({{ valid_to_inclusive }}, date '9999-12-31')",
        "assert_business_validity_backdated",
    ),
]
# A floor stated as a number, never read from the folder: the macro tests that exist.
MACRO_TEST_FLOOR = 6

# Fixture models for the generic silver tests: each model's rows, and the one test planted to
# fail on it. `clean` carries every test and must pass them all. A row is
# (key, _valid_from, _valid_to, _is_current), with E for the epoch and Z for the end of time.
E, Z = "{{ scd2_epoch() }}", "{{ scd2_end() }}"
T1, T2 = "timestamptz '2026-07-21 09:00:00+00'", "timestamptz '2026-07-22 09:00:00+00'"
CLEAN_VERSIONS = [(1, E, T1, "false"), (1, T1, Z, "true"), (2, E, Z, "true"), (3, E, T2, "false")]
SILVER_FIXTURES: dict[str, tuple[list, str | None]] = {
    "clean": (CLEAN_VERSIONS, None),
    "duplicate": ([(1, E, Z, "true"), (1, E, Z, "true")], "silver_unique_key"),
    "null_key": ([(None, E, Z, "true")], "silver_not_null"),
    "short": (CLEAN_VERSIONS, "silver_min_rows"),
    "gap": ([(1, E, T1, "false"), (1, T2, Z, "true")], "silver_scd2_intervals"),
    "overlap": ([(1, E, T2, "false"), (1, T1, Z, "true")], "silver_scd2_intervals"),
    "late_first": ([(1, T1, Z, "true")], "silver_scd2_intervals"),
    "early_current": ([(1, E, T1, "true"), (1, T1, Z, "true")], "silver_scd2_intervals"),
    "open_closed": ([(1, E, Z, "false")], "silver_scd2_intervals"),
    "disagree": (CLEAN_VERSIONS, "silver_rereads_agree"),
    "unversioned": ([(1, E, Z, "true")], "silver_observations_versioned"),
    # A key soft-deleted at T1 and restored at T2: the gap directly after the deleted version is
    # allowed, and every test passes.
    "restored": ([(1, E, T1, "false"), (1, T2, Z, "true")], None),
    # The same gap, but the upstream's delete is at T0, not where the gap starts.
    "gap_elsewhere": ([(1, E, T1, "false"), (1, T2, Z, "true")], "silver_scd2_intervals"),
}
# The upstream each fixture's tests read: (key, updated_at, value, batch, is_deleted). By default
# two batches of one version, which agree, and a second key.
T20 = "timestamptz '2026-07-20 00:00:00+00'"
T0_DELETE = "timestamptz '2026-07-20 12:00:00+00'"
UPSTREAM_AGREE = [
    (1, T20, "a", "b-01", "false"),
    (1, T20, "a", "b-02", "false"),
    (2, T20, "b", "b-01", "false"),
]
UPSTREAMS = {
    "disagree": [
        (1, T20, "a", "b-01", "false"),
        (1, T20, "z", "b-02", "false"),
        (2, T20, "b", "b-01", "false"),
    ],
    "restored": [
        (1, T20, "a", "b-01", "false"),
        (1, T1, "a", "b-02", "true"),
        (1, T2, "a", "b-03", "false"),
    ],
    "gap_elsewhere": [
        (1, T20, "a", "b-01", "false"),
        (1, T0_DELETE, "a", "b-02", "true"),
        (1, T2, "a", "b-03", "false"),
    ],
}


def _values(rows: list, width: int) -> str:
    def cell(value):
        return "null::bigint" if value is None else str(value)

    if not rows:
        return "select " + ", ".join(["null"] * width) + " where false"
    return "values " + ", ".join("(" + ", ".join(cell(v) for v in row) + ")" for row in rows)


# Facts against a dimension's versions, for `silver_resolves_one_version`: key 1 has two versions
# split at T1, key 2 one version closed at T2 by a soft delete. A fact row is (fact_id, k, at).
T0 = "timestamptz '2026-07-20 12:00:00+00'"
T3 = "timestamptz '2026-07-23 09:00:00+00'"
DIMENSION = [(1, E, T1), (1, T1, Z), (2, E, T2)]
OVERLAPPING = [(1, E, T2), (1, T1, Z), (2, E, T2)]
RESOLVE_FIXTURES: dict[str, tuple[list, list, bool]] = {
    # Two facts with one key at one instant are two facts, each resolving to one version.
    "resolves": ([(1, 1, T0), (2, 1, T0), (3, 1, T1), (4, 2, T0)], DIMENSION, False),
    "after_delete": ([(1, 2, T3)], DIMENSION, True),
    "before_first": ([(1, 3, T0)], DIMENSION + [(3, T2, Z)], True),
    "overlapping": ([(1, 1, T1)], OVERLAPPING, True),
}
# Converted rows for `silver_fx_provenance`: (id, amount, currency, amount_eur, fx_rate,
# fx_rate_date, carried, missing, provisional).
D = "date '2026-07-20'"
CONVERTED = [
    (1, "100.00", "'EUR'", "100.00", "1", "null", "false", "false", "false"),
    (2, "100.00", "'USD'", "87.5197", "1.1426", D, "false", "false", "true"),
    (3, "100.00", "'USD'", "null", "null", "null", "false", "true", "false"),
]
PROVENANCE_FIXTURES: dict[str, tuple[list, bool]] = {
    "converted": (CONVERTED, False),
    "missing_as_zero": (CONVERTED[:2] + [(3, "100.00", "'USD'", "0", "null", "null",
                                          "false", "true", "false")], True),
    "missing_as_original": (CONVERTED[:2] + [(3, "100.00", "'USD'", "100.00", "null", "null",
                                              "false", "true", "false")], True),
    "eur_with_a_date": ([(1, "100.00", "'EUR'", "100.00", "1", D, "false", "false", "false")],
                        True),
    "missing_provisional": ([(3, "100.00", "'USD'", "null", "null", "null", "false", "true",
                              "true")], True),
}  # fmt: skip


# Band intervals for `silver_intervals_contiguous`: (k, valid_from, valid_to) as dates.
J1, J2, J3, OPEN = (
    "date '2000-01-01'",
    "date '2018-01-01'",
    "date '2025-01-01'",
    "date '9999-12-31'",
)
INTERVAL_FIXTURES: dict[str, tuple[list, bool]] = {
    "contiguous": ([(1, J1, J2), (1, J2, J3), (1, J3, OPEN), (2, J2, OPEN)], False),
    "interval_gap": ([(1, J1, J2), (1, J3, OPEN)], True),
    "interval_overlap": ([(1, J1, J3), (1, J2, OPEN)], True),
    "closed_last": ([(1, J1, J2), (1, J2, J3)], True),
    "inverted": ([(1, J2, J1), (1, J1, OPEN)], True),
}


# A derived flag for `silver_flag_split`, floors 2 true and 1 false: (id, flag).
SPLIT_FIXTURES: dict[str, tuple[list, bool]] = {
    "split": ([(1, "true"), (2, "true"), (3, "false")], False),
    "all_one_way": ([(1, "true"), (2, "true"), (3, "true")], True),
    "too_few_true": ([(1, "true"), (2, "false"), (3, "false")], True),
    "null_flag": ([(1, "true"), (2, "true"), (3, "false"), (4, "null::boolean")], True),
}


def fact_fixture_files() -> tuple[dict[str, str], dict[tuple[str, str], str]]:
    """Fixture facts for the resolution and provenance tests, and the status each must reach."""
    files, expected = {}, {}
    properties = ["version: 2", "models:"]
    for name, (facts, versions, fails) in RESOLVE_FIXTURES.items():
        files[f"models/dimension_{name}.sql"] = (
            f"select * from ({_values(versions, 3)}) as t (k, _valid_from, _valid_to)\n"
        )
        files[f"models/facts_{name}.sql"] = (
            f"select * from ({_values(facts, 3)}) as t (fact_id, k, event_at)\n"
        )
        properties += [
            f"  - name: facts_{name}",
            "    columns:",
            "      - name: k",
            "        data_tests:",
            "          - silver_resolves_one_version:",
            f"              arguments: {{to: \"ref('dimension_{name}')\", to_column: k, "
            "at: event_at}",
        ]
        expected[(f"facts_{name}", "silver_resolves_one_version")] = "fail" if fails else "pass"
    columns = "(id, amount, currency, amount_eur, fx_rate, fx_rate_date, fx_is_carried, " \
        "fx_is_missing, fx_is_provisional)"  # fmt: skip
    for name, (rows, fails) in PROVENANCE_FIXTURES.items():
        typed = [
            (i, f"cast({a} as decimal(18,4))", c, f"cast({e} as decimal(18,4))",
             f"cast({r} as decimal(18,8))", f"cast({d} as date)", ca, m, pr)
            for i, a, c, e, r, d, ca, m, pr in rows
        ]  # fmt: skip
        files[f"models/converted_{name}.sql"] = (
            f"select * from ({_values(typed, 9)}) as t {columns}\n"
        )
        properties += [
            f"  - name: converted_{name}",
            "    data_tests:",
            "      - silver_fx_provenance: {arguments: {key: id, amount: amount, "
            "currency: currency}}",
        ]
        expected[(f"converted_{name}", "silver_fx_provenance")] = "fail" if fails else "pass"
    for name, (rows, fails) in INTERVAL_FIXTURES.items():
        files[f"models/bands_{name}.sql"] = (
            f"select * from ({_values(rows, 3)}) as t (k, valid_from, valid_to)\n"
        )
        properties += [
            f"  - name: bands_{name}",
            "    data_tests:",
            "      - silver_intervals_contiguous: {arguments: {key: [k], valid_from: valid_from, "
            "valid_to: valid_to}}",
        ]
        expected[(f"bands_{name}", "silver_intervals_contiguous")] = "fail" if fails else "pass"
    for name, (rows, fails) in SPLIT_FIXTURES.items():
        files[f"models/flags_{name}.sql"] = (
            f"select * from ({_values(rows, 2)}) as t (id, is_customer_initiated)\n"
        )
        properties += [
            f"  - name: flags_{name}",
            "    columns:",
            "      - name: is_customer_initiated",
            "        data_tests:",
            "          - silver_flag_split: {arguments: {minimum_true: 2, minimum_false: 1}}",
        ]
        expected[(f"flags_{name}", "silver_flag_split")] = "fail" if fails else "pass"
    files["models/facts.yml"] = "\n".join(properties) + "\n"
    return files, expected


def silver_fixture_files() -> dict[str, str]:
    """The fixture models and their properties, as files of the macro project."""
    files = {}
    properties = ["version: 2", "models:"]
    for name, (rows, planted) in SILVER_FIXTURES.items():
        upstream = UPSTREAMS.get(name, UPSTREAM_AGREE)
        files[f"models/upstream_{name}.sql"] = (
            "select * from ("
            f"{_values([(k, at, repr(v), repr(b), d) for k, at, v, b, d in upstream], 5)}) "
            "as t (k, updated_at, v, _batch_id, is_deleted)\n"
        )
        files[f"models/silver_{name}.sql"] = (
            f"select * from ({_values(rows, 4)}) as t (k, _valid_from, _valid_to, _is_current)\n"
        )
        tests = {
            "silver_unique_key": "{arguments: {columns: [k, _valid_from]}}",
            "silver_not_null": "{arguments: {columns: [k, _valid_from]}}",
            "silver_min_rows": "{arguments: {minimum: " + ("5" if name == "short" else "1") + "}}",
            "silver_scd2_intervals": (
                "{arguments: {key: [k], upstream: \"ref('upstream_" + name + "')\"}}"
            ),
            "silver_rereads_agree": (
                "{arguments: {upstream: \"ref('upstream_" + name + "')\", key: [k]}}"
            ),
            "silver_observations_versioned": (
                "{arguments: {upstream: \"ref('upstream_" + name + "')\", key: [k]}}"
            ),
        }
        chosen = tests if planted is None else {planted: tests[planted]}
        properties += [f"  - name: silver_{name}", "    data_tests:"]
        properties += [f"      - {test}: {arguments}" for test, arguments in chosen.items()]
    files["models/fixtures.yml"] = "\n".join(properties) + "\n"
    return files


def prove_silver_tests(work: Path) -> list[str]:
    """Every generic silver test passes on the clean fixture and fails where it was planted."""
    home = macro_project(work)
    facts, expected_facts = fact_fixture_files()
    for relative, text in {**silver_fixture_files(), **facts}.items():
        path = home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    dbt(home, work, work / "lake", "build", "--select", "path:models")
    manifest = json.loads((work / "target" / "manifest.json").read_text(encoding="utf-8"))
    results = json.loads((work / "target" / "run_results.json").read_text(encoding="utf-8"))
    seen: dict[tuple[str, str], str] = {}
    failures: list[str] = []
    for result in results["results"]:
        node = manifest["nodes"].get(result["unique_id"], {})
        if node.get("resource_type") == "test" and node.get("test_metadata"):
            model = node["attached_node"].split(".")[-1]
            key = (model, node["test_metadata"]["name"])
            if key in expected_facts:
                want = expected_facts.pop(key)
                if result["status"] != want:
                    failures.append(f"fixture {model}: {key[1]} was {result['status']}, "
                                    f"expected {want}")  # fmt: skip
                elif want == "fail":
                    print(f"dbt-prove: {key[1]} fails on the planted {model} fixture")
                continue
            seen[(model.removeprefix("silver_"), key[1])] = result["status"]
    for model, test in expected_facts:
        failures.append(f"fixture {model}: {test} did not run")
    clean = [k for k in seen if k[0] == "clean"]
    if len(clean) < 6:
        return failures + [f"the clean silver fixture ran {len(clean)} test(s), expected 6"]
    for (model, test), status in sorted(seen.items()):
        planted = SILVER_FIXTURES[model][1]
        expected = "fail" if test == planted else "pass"
        if status != expected:
            failures.append(f"silver fixture {model}: {test} was {status}, expected {expected}")
        elif expected == "fail":
            print(f"dbt-prove: {test} fails on the planted {model} fixture")
    if not failures:
        print("dbt-prove: every generic silver test passes on the clean fixture")
    return failures


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
