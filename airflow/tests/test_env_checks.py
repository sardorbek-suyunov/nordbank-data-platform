"""Unit tests for the environment file generation and validation.

The inline-comment case has its own test because it is the bug that actually happened: a value
that carried its explanatory comment reached a container and the signature did not match.
"""

from __future__ import annotations

import base64

import check_env
import init_env
import pytest

TEMPLATE = """\
# A section
NORDBANK_ENV=dev                      # not a secret
SOURCE_READ_PASSWORD=__GENERATE__     # password for the extraction role
MINIO_ROOT_PASSWORD=__GENERATE__      # password for MinIO
AWS_SECRET_ACCESS_KEY=__GENERATE__    # generated to match MINIO_ROOT_PASSWORD
AIRFLOW_FERNET_KEY=__GENERATE__       # 32 random bytes
AIRFLOW_CONN_NORDBANK_SOURCE_DB=postgresql://reader:__GENERATE__@src:5432/nb  # uri
FRED_API_KEY=__EXTERNAL__             # issued by FRED
"""


def test_generation_replaces_every_sentinel() -> None:
    content, generated = init_env.generate(TEMPLATE)

    assert init_env.GENERATE not in content
    assert set(generated) == {
        "SOURCE_READ_PASSWORD",
        "MINIO_ROOT_PASSWORD",
        "AWS_SECRET_ACCESS_KEY",
        "AIRFLOW_FERNET_KEY",
        "AIRFLOW_CONN_NORDBANK_SOURCE_DB",
    }


def test_derived_values_match_their_source() -> None:
    content, generated = init_env.generate(TEMPLATE)

    assert generated["AWS_SECRET_ACCESS_KEY"] == generated["MINIO_ROOT_PASSWORD"]
    assert generated["SOURCE_READ_PASSWORD"] in content
    uri = next(
        line for line in content.splitlines() if line.startswith("AIRFLOW_CONN_NORDBANK_SOURCE_DB")
    )
    assert generated["SOURCE_READ_PASSWORD"] in uri


def test_generated_file_carries_no_inline_comments() -> None:
    content, _ = init_env.generate(TEMPLATE)

    for line in content.splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        assert " #" not in line, f"generated .env kept an inline comment: {line}"


def test_external_sentinel_is_left_alone() -> None:
    content, generated = init_env.generate(TEMPLATE)

    assert "FRED_API_KEY=__EXTERNAL__" in content
    assert "FRED_API_KEY" not in generated


def test_generated_fernet_key_is_thirty_two_bytes() -> None:
    _, generated = init_env.generate(TEMPLATE)

    assert len(base64.urlsafe_b64decode(generated["AIRFLOW_FERNET_KEY"])) == 32


def test_a_valid_env_passes() -> None:
    content, _ = init_env.generate(TEMPLATE)
    failures, notes = check_env.check(TEMPLATE, content)

    assert failures == []
    assert any("FRED_API_KEY" in note for note in notes)


def test_missing_variable_is_named() -> None:
    content, _ = init_env.generate(TEMPLATE)
    without = "\n".join(
        line for line in content.splitlines() if not line.startswith("MINIO_ROOT_PASSWORD")
    )

    failures, _ = check_env.check(TEMPLATE, without)

    assert any("MINIO_ROOT_PASSWORD" in failure and "missing" in failure for failure in failures)


def test_unreplaced_sentinel_is_named() -> None:
    failures, _ = check_env.check(TEMPLATE, "SOURCE_READ_PASSWORD=__GENERATE__")

    assert any(
        "SOURCE_READ_PASSWORD" in failure and "__GENERATE__" in failure for failure in failures
    )


def test_leaked_inline_comment_is_named() -> None:
    leaked = "SOURCE_READ_PASSWORD=hunter2 # password for the extraction role"

    failures, _ = check_env.check(TEMPLATE, leaked)

    assert any(
        "SOURCE_READ_PASSWORD" in failure and "inline comment" in failure for failure in failures
    )


@pytest.mark.parametrize(
    "value",
    [
        base64.urlsafe_b64encode(b"too short").decode(),
        "not base64 at all!!",
        base64.urlsafe_b64encode(b"x" * 64).decode(),
    ],
)
def test_bad_fernet_keys_are_named(value: str) -> None:
    failures, _ = check_env.check(TEMPLATE, f"AIRFLOW_FERNET_KEY={value}")

    assert any(check_env.FERNET_KEY in failure for failure in failures)


def test_failures_never_print_the_value() -> None:
    leaked = "SOURCE_READ_PASSWORD=hunter2 # password"

    failures, _ = check_env.check(TEMPLATE, leaked)

    assert all("hunter2" not in failure for failure in failures)


def test_a_generated_secret_never_begins_with_a_dash(monkeypatch) -> None:
    """One `token_urlsafe` value in sixty-four begins with `-`, which a CLI reads as an option."""
    import init_env

    drawn = iter(["-leading-dash-value", "-another", "clean-value"])
    monkeypatch.setattr(init_env.secrets, "token_urlsafe", lambda _n: next(drawn))
    assert init_env.secret("POSTGRES_AIRFLOW_PASSWORD") == "clean-value"
    monkeypatch.undo()
    assert all(not init_env.secret("MINIO_ROOT_PASSWORD").startswith("-") for _ in range(2000))
