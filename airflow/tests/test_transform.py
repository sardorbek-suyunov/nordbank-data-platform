"""dbt's child process gets the lake credentials as the profile reads them, and nothing else."""

import pytest
from nordbank_ops.transform import BRONZE, PROJECT_DIR, command, lake_settings


def test_the_lake_connection_becomes_the_profiles_variables():
    settings = lake_settings(
        "minio", "not-a-real-secret", {"endpoint_url": "http://minio:9000", "region_name": "x"}
    )
    assert settings == {
        "DBT_ENV_SECRET_LAKE_KEY_ID": "minio",
        "DBT_ENV_SECRET_LAKE_SECRET": "not-a-real-secret",
        "LAKE_S3_ENDPOINT": "minio:9000",
        "LAKE_S3_REGION": "x",
    }
    # Only the two credentials are secrets, and both carry the prefix dbt scrubs from its logs.
    assert [k for k in settings if "SECRET" in k or "KEY" in k] == [
        "DBT_ENV_SECRET_LAKE_KEY_ID",
        "DBT_ENV_SECRET_LAKE_SECRET",
    ]


def test_an_endpoint_the_profile_cannot_use_is_refused():
    with pytest.raises(RuntimeError):
        lake_settings("minio", "s", {"endpoint_url": "https://minio:9000"})
    with pytest.raises(RuntimeError):
        lake_settings("minio", "s", {})


def test_the_command_names_the_project_and_the_bronze_selection():
    assert command(["build", "--select", BRONZE]) == [
        "/opt/dbt/bin/dbt",
        "build",
        "--select",
        "path:models/bronze",
        "--project-dir",
        PROJECT_DIR,
    ]
