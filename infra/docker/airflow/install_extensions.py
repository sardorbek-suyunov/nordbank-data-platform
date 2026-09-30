"""Install the DuckDB extensions the platform uses, at image build (spec 007 section 2).

Run by dbt's environment, whose DuckDB is the same version as Airflow's, into the directory
`DUCKDB_EXTENSION_DIRECTORY` names. Nothing installs or downloads an extension at run time: the
profile turns automatic install off, and a test loads httpfs from here with no network at all.
"""

import os

import duckdb

EXTENSIONS = ("httpfs",)

directory = os.environ["DUCKDB_EXTENSION_DIRECTORY"]
connection = duckdb.connect(config={"extension_directory": directory})
for name in EXTENSIONS:
    connection.execute(f"install {name}")
    connection.execute(f"load {name}")
    (path,) = connection.execute(
        "select install_path from duckdb_extensions() where extension_name = ?", [name]
    ).fetchone()
    print(f"duckdb {duckdb.__version__}: {name} installed at {path}")
