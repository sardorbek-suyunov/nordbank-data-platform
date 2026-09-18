"""Smoke tests against a running stack.

Marked `integration` and run inside an Airflow container, because the warehouse file lives on a
named volume and the service names only resolve on the compose network. Each test skips when
the thing it needs is not reachable, so running them outside a stack reports honestly rather
than failing noisily.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.integration

BUCKET = os.environ.get("LAKE_BUCKET", "nordbank-lake")


def _source_cursor():
    psycopg2 = pytest.importorskip("psycopg2")
    dsn = os.environ.get("AIRFLOW_CONN_NORDBANK_SOURCE_DB")
    if not dsn:
        pytest.skip("AIRFLOW_CONN_NORDBANK_SOURCE_DB is not set")
    try:
        return psycopg2.connect(dsn, connect_timeout=5)
    except psycopg2.OperationalError as exc:
        pytest.skip(f"source database unreachable: {exc}")


def _lake_client():
    pytest.importorskip("boto3")
    import sys

    sys.path.insert(0, "/opt/airflow/plugins")
    from nordbank_ops import clients

    try:
        return clients.lake_client()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"lake client unavailable: {exc}")


def test_source_database_has_its_schemas() -> None:
    from nordbank_ops import source_db

    connection = _source_cursor()
    try:
        with connection.cursor() as cursor:
            assert source_db.assert_schemas(cursor) == ["core", "ref"]
            assert source_db.current_user(cursor) == os.environ.get(
                "SOURCE_READ_USER", "nordbank_reader"
            )
    finally:
        connection.close()


def test_extraction_role_cannot_create_a_table() -> None:
    from nordbank_ops import source_db

    connection = _source_cursor()
    try:
        with connection.cursor() as cursor:
            sqlstate = source_db.assert_cannot_create(cursor, "core")
        assert sqlstate == "42501", f"expected insufficient_privilege, got {sqlstate}"
    finally:
        connection.rollback()
        connection.close()


def test_pool_warehouse_access_exists_with_one_slot() -> None:
    psycopg2 = pytest.importorskip("psycopg2")
    # The credentials live in the SQLAlchemy URL the services already run with, so the test
    # needs no extra secret in the environment.
    url = os.environ.get("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN", "")
    if not url:
        pytest.skip("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN is not set")
    dsn = url.replace("postgresql+psycopg2://", "postgresql://", 1)
    try:
        connection = psycopg2.connect(dsn, connect_timeout=5)
    except psycopg2.OperationalError as exc:
        pytest.skip(f"airflow metadata database unreachable: {exc}")
    try:
        with connection.cursor() as cursor:
            cursor.execute("select slots from slot_pool where pool = 'warehouse_access'")
            row = cursor.fetchone()
            assert row is not None, "pool warehouse_access does not exist"
            assert row[0] == 1, f"pool warehouse_access has {row[0]} slots, expected 1"
    finally:
        connection.close()


def test_bucket_has_its_prefixes() -> None:
    from nordbank_ops import lake

    client = _lake_client()
    assert lake.assert_prefixes(client, BUCKET) == ["bronze", "quarantine"]


def test_batch_id_makes_two_writes_two_keys() -> None:
    from nordbank_ops import lake

    client = _lake_client()
    result = lake.probe_bucket(client, BUCKET, ("smoke-a", "smoke-b"), "2026-09-18")
    assert len(set(result["keys"])) == 2
    assert len(result["found"]) == 2


def test_anonymous_access_is_denied() -> None:
    endpoint = os.environ.get("AWS_ENDPOINT_URL", "http://minio:9000")
    request = urllib.request.Request(f"{endpoint}/{BUCKET}/", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            body = response.read().decode(errors="ignore")
        pytest.fail(f"anonymous listing succeeded with {response.status}: {body[:200]}")
    except urllib.error.HTTPError as exc:
        assert exc.code == 403, f"expected 403 for anonymous access, got {exc.code}"
    except urllib.error.URLError as exc:
        pytest.skip(f"MinIO unreachable: {exc}")


def test_warehouse_has_the_six_schemas() -> None:
    pytest.importorskip("duckdb")
    from nordbank_ops import warehouse

    if not os.environ.get("DUCKDB_PATH"):
        pytest.skip("DUCKDB_PATH is not set")

    with warehouse.connect(read_only=True, attempts=3) as connection:
        assert warehouse.missing_schemas(connection) == []


def test_airflow_api_reports_healthy() -> None:
    url = "http://airflow-apiserver:8080/api/v2/monitor/health"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310
            payload = json.loads(response.read().decode())
    except (urllib.error.URLError, TimeoutError) as exc:
        pytest.skip(f"Airflow API unreachable: {exc}")

    assert payload["metadatabase"]["status"] == "healthy"
    assert payload["scheduler"]["status"] == "healthy"
