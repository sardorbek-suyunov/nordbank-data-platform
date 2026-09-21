"""Unit tests for how the driver resolves where to connect and what it says when it cannot.

The failure text is tested rather than left to be read once, because it is the mitigation for a
real failure: a native PostgreSQL bound to the published port shadows the container silently,
Docker reports the mapping either way, and the driver then reaches the wrong server. ADR 0012
records the incident. A message that does not name that cause sends the next person to the
wrong place, so the naming is a behaviour and behaviours get tests.

No database and no Airflow: these run in `make test`.
"""

from __future__ import annotations

import pytest
from source_db_driver import EXPECTED_SCHEMAS, Settings, SourceDatabaseError, assert_identity
from source_db_driver import resolve as resolve_settings

ENV = """
POSTGRES_SOURCE_DB=nordbank
POSTGRES_SOURCE_PORT=15432
SOURCE_APP_USER=nordbank_app
SOURCE_APP_PASSWORD=secret
"""


@pytest.fixture
def env_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text(ENV, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _clear_overrides(monkeypatch):
    for key in (
        "NORDBANK_SOURCE_HOST",
        "NORDBANK_SOURCE_PORT",
        "POSTGRES_SOURCE_DB",
        "POSTGRES_SOURCE_PORT",
        "SOURCE_APP_USER",
        "SOURCE_APP_PASSWORD",
    ):
        monkeypatch.delenv(key, raising=False)


def test_the_file_supplies_the_host_default_and_the_published_port(env_file):
    settings = resolve_settings(env_file)
    assert (settings.host, settings.port) == ("localhost", 15432)
    assert settings.user == "nordbank_app"


def test_the_environment_wins_over_the_file(env_file, monkeypatch):
    # This is how the DAG reaches the compose service without a second copy of the credentials.
    monkeypatch.setenv("NORDBANK_SOURCE_HOST", "postgres-source")
    monkeypatch.setenv("NORDBANK_SOURCE_PORT", "5432")
    settings = resolve_settings(env_file)
    assert (settings.host, settings.port) == ("postgres-source", 5432)
    assert settings.where == "postgres-source:5432/nordbank"


def test_a_missing_credential_names_itself_and_the_remedy(tmp_path):
    path = tmp_path / ".env"
    path.write_text("POSTGRES_SOURCE_DB=nordbank\n", encoding="utf-8")
    with pytest.raises(SourceDatabaseError) as error:
        resolve_settings(path)
    assert "SOURCE_APP_USER" in str(error.value)
    assert "make init-env" in str(error.value)


class _Cursor:
    def __init__(self, row):
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, *_args, **_kwargs):
        return None

    def fetchone(self):
        return self._row


class _Connection:
    def __init__(self, row):
        self._row = row

    def cursor(self):
        return _Cursor(self._row)


LOCAL = Settings("localhost", 5432, "nordbank", "nordbank_app", "secret")
REMOTE = Settings("postgres-source", 5432, "nordbank", "nordbank_app", "secret")


def test_identity_passes_when_every_schema_is_present():
    assert_identity(_Connection((16, len(EXPECTED_SCHEMAS))), LOCAL)


def test_identity_failure_over_localhost_names_the_port_collision():
    with pytest.raises(SourceDatabaseError) as error:
        assert_identity(_Connection((18, 0)), LOCAL)
    message = str(error.value)
    assert "not the Nordbank source" in message
    assert "Another PostgreSQL may be bound to port 5432" in message
    assert "POSTGRES_SOURCE_PORT" in message


def test_identity_failure_inside_the_stack_does_not_blame_a_port_collision():
    # Reached by service name over the compose network, so no published port is involved and
    # the collision hint would send the reader somewhere useless.
    with pytest.raises(SourceDatabaseError) as error:
        assert_identity(_Connection((16, 1)), REMOTE)
    message = str(error.value)
    assert "Another PostgreSQL" not in message
    assert "make schema-apply" in message


def test_an_unexpected_major_version_is_reported_and_not_fatal(capsys):
    assert_identity(_Connection((18, len(EXPECTED_SCHEMAS))), LOCAL)
    assert "measured against 16" in capsys.readouterr().err
