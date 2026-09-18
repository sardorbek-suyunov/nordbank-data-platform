"""Airflow connections to concrete clients.

This is the only module here that imports Airflow, psycopg2 or boto3, so everything else stays
unit-testable without a stack or an Airflow installation.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

SOURCE_CONN_ID = "nordbank_source_db"
LAKE_CONN_ID = "nordbank_lake"


def _connection(conn_id: str) -> Any:
    try:
        from airflow.sdk.bases.hook import BaseHook  # Airflow 3 task SDK
    except ImportError:  # pragma: no cover - older layouts
        from airflow.hooks.base import BaseHook

    return BaseHook.get_connection(conn_id)


@contextmanager
def source_cursor(conn_id: str = SOURCE_CONN_ID) -> Iterator[Any]:
    import psycopg2

    conn = _connection(conn_id)
    connection = psycopg2.connect(
        host=conn.host,
        port=conn.port or 5432,
        dbname=(conn.schema or "nordbank"),
        user=conn.login,
        password=conn.password,
        connect_timeout=10,
    )
    try:
        with connection.cursor() as cursor:
            yield cursor
    finally:
        connection.rollback()
        connection.close()


def lake_client(conn_id: str = LAKE_CONN_ID) -> Any:
    import boto3

    conn = _connection(conn_id)
    extra = conn.extra_dejson
    return boto3.client(
        "s3",
        endpoint_url=extra.get("endpoint_url"),
        aws_access_key_id=conn.login,
        aws_secret_access_key=conn.password,
        region_name=extra.get("region_name", "us-east-1"),
    )


def lake_bucket() -> str:
    bucket = os.environ.get("LAKE_BUCKET")
    if not bucket:
        raise RuntimeError("LAKE_BUCKET is not set")
    return bucket
