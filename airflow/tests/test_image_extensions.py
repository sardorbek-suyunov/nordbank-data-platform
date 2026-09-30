"""The image carries httpfs for its one DuckDB version, loadable with no network (spec 007).

These run inside the project image, where `make test-offline` runs them with `--network none`,
so a pass means the extension came from the build and nothing was downloaded. On a host without
the image's extension directory they skip, and say why.
"""

import os
import subprocess
from pathlib import Path

import duckdb
import pytest

DIRECTORY = os.environ.get("DUCKDB_EXTENSION_DIRECTORY", "/opt/duckdb/extensions")
DBT_PYTHON = Path("/opt/dbt/bin/python")

in_the_image = pytest.mark.skipif(
    not Path(DIRECTORY).is_dir() or not DBT_PYTHON.exists(),
    reason="runs in the project image, where the build installed the extensions",
)


@in_the_image
def test_httpfs_loads_from_the_build_time_install_with_automatic_install_off():
    connection = duckdb.connect(
        config={"extension_directory": DIRECTORY, "autoinstall_known_extensions": False}
    )
    # What the dbt profile does for `extensions: [httpfs]`: install, then load. The install is a
    # no-op when the extension is present, and needs the network when it is not.
    connection.install_extension("httpfs")
    connection.load_extension("httpfs")
    (loaded,) = connection.execute(
        "select loaded from duckdb_extensions() where extension_name = 'httpfs'"
    ).fetchone()
    assert loaded
    installed = list(Path(DIRECTORY).rglob("httpfs.duckdb_extension"))
    assert len(installed) >= 1
    assert f"v{duckdb.__version__}" in {part for path in installed for part in path.parts}


@in_the_image
def test_dbt_and_airflow_run_the_same_duckdb():
    reported = subprocess.run(
        [str(DBT_PYTHON), "-c", "import duckdb; print(duckdb.__version__)"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert reported == duckdb.__version__
