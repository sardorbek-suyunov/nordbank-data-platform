"""Generate .env from .env.example, replacing every __GENERATE__ sentinel.

The template holds no working secret, so something has to make one. Generation is stdlib only:
a Fernet key is 32 random bytes in urlsafe base64, which needs no cryptography package, and
Airflow does not import on every host this runs on.

The generated file carries values without inline comments. Comments belong in the template; a
comment in .env is how a parsing mistake once handed a container a password with an
explanation stapled to it.
"""

from __future__ import annotations

import base64
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / ".env.example"
TARGET = ROOT / ".env"

GENERATE = "__GENERATE__"
EXTERNAL = "__EXTERNAL__"

# Variables whose generated value must equal another variable's, because two services have to
# agree on it. Order matters: the source is generated first.
DERIVED = {
    "AWS_SECRET_ACCESS_KEY": "MINIO_ROOT_PASSWORD",
    "AIRFLOW_CONN_NORDBANK_SOURCE_DB": "SOURCE_READ_PASSWORD",
    # ops_source_tick writes to the source and so authenticates as the application role, not
    # as the SELECT-only one every extraction path uses. The distinction is the subject of a
    # paragraph in docs/architecture.md rather than an inference from a connection id.
    "AIRFLOW_CONN_NORDBANK_SOURCE_SIMULATOR": "SOURCE_APP_PASSWORD",
    "AIRFLOW_CONN_NORDBANK_LAKE": "MINIO_ROOT_PASSWORD",
}


def fernet_key() -> str:
    """32 random bytes, urlsafe base64, which is exactly what Airflow expects."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def secret(name: str) -> str:
    """A generated credential that no command line can mistake for an option.

    `token_urlsafe` draws from an alphabet that includes `-`, and measured, one value in
    sixty-four begins with it. A password beginning with a dash reaches `airflow users create
    --password` and `mc alias set` as an argument, where it is parsed as an option: CI's stack
    job failed once on `argument -p/--password: expected one argument` with a freshly generated
    `.env`. So a value is drawn again until it does not begin with one.
    """
    if name == "AIRFLOW_FERNET_KEY":
        return fernet_key()
    value = secrets.token_urlsafe(24)
    while value.startswith("-"):
        value = secrets.token_urlsafe(24)
    return value


def strip_comment(value: str) -> str:
    marker = value.find(" #")
    return (value[:marker] if marker != -1 else value).strip()


def generate(template: str) -> tuple[str, dict[str, str]]:
    generated: dict[str, str] = {}
    lines: list[str] = []

    # First pass: everything that is not derived from another variable.
    for raw in template.splitlines():
        line = raw.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            lines.append(line)
            continue

        key, _, value = line.partition("=")
        key = key.strip()
        value = strip_comment(value)

        if GENERATE in value and key not in DERIVED:
            generated[key] = secret(key)
            value = value.replace(GENERATE, generated[key])

        lines.append(f"{key}={value}")

    # Second pass: the ones that must match a value generated above.
    for index, line in enumerate(lines):
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, _, value = line.partition("=")
        if key in DERIVED and GENERATE in value:
            source = DERIVED[key]
            if source not in generated:
                raise RuntimeError(f"{key} derives from {source}, which was not generated")
            generated[key] = generated[source]
            lines[index] = f"{key}={value.replace(GENERATE, generated[source])}"

    return "\n".join(lines) + "\n", generated


def main() -> int:
    if TARGET.exists():
        print(
            f"init-env: {TARGET.name} already exists and will not be overwritten.\n"
            "Delete it first if you want a new one; its credentials are the ones the running "
            "stack was built with.",
            file=sys.stderr,
        )
        return 1

    content, generated = generate(TEMPLATE.read_text(encoding="utf-8"))
    TARGET.write_text(content, encoding="utf-8", newline="\n")

    print(f"init-env: wrote {TARGET.name} with {len(generated)} generated values:")
    for key in sorted(generated):
        derived = f" (matches {DERIVED[key]})" if key in DERIVED else ""
        print(f"  {key}{derived}")

    external = [
        line.partition("=")[0]
        for line in content.splitlines()
        if EXTERNAL in line and not line.lstrip().startswith("#")
    ]
    if external:
        print(f"init-env: still {EXTERNAL}, to be supplied when a milestone needs them:")
        for key in external:
            print(f"  {key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
