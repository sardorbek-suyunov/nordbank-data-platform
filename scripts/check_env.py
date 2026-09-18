"""Validate .env against .env.example before anything starts.

Both failures in M1 were environment-variable failures: a password that carried its own
explanatory comment into a container, and a uid that looked reasonable and broke the image. So
this is a control rather than two point fixes, and `make up` runs it before it starts anything.
"""

from __future__ import annotations

import base64
import binascii
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / ".env.example"
TARGET = ROOT / ".env"

GENERATE = "__GENERATE__"
EXTERNAL = "__EXTERNAL__"
FERNET_KEY = "AIRFLOW_FERNET_KEY"
FERNET_BYTES = 32


def parse(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def fernet_problem(value: str) -> str | None:
    try:
        decoded = base64.urlsafe_b64decode(value)
    except (binascii.Error, ValueError) as exc:
        return f"is not urlsafe base64 ({exc})"
    if len(decoded) != FERNET_BYTES:
        return f"decodes to {len(decoded)} bytes, expected {FERNET_BYTES}"
    return None


def check(template_text: str, env_text: str) -> tuple[list[str], list[str]]:
    """Returns (failures, notes). Values are never printed; only variable names."""
    template = parse(template_text)
    env = parse(env_text)
    failures: list[str] = []
    notes: list[str] = []

    for key in template:
        if key not in env:
            failures.append(f"{key}: present in .env.example, missing from .env")

    for key, value in env.items():
        if GENERATE in value:
            failures.append(f"{key}: still {GENERATE}; run `make init-env` or set it by hand")
        elif " #" in value:
            failures.append(
                f"{key}: value carries an inline comment; the comment belongs in .env.example"
            )
        elif EXTERNAL in value:
            notes.append(f"{key}: still {EXTERNAL}, supply it when a milestone needs it")

    fernet = env.get(FERNET_KEY, "")
    if fernet and GENERATE not in fernet:
        problem = fernet_problem(fernet)
        if problem:
            failures.append(f"{FERNET_KEY}: {problem}")

    return failures, notes


def main() -> int:
    if not TARGET.exists():
        print("check-env: .env does not exist; run `make init-env`", file=sys.stderr)
        return 1

    failures, notes = check(
        TEMPLATE.read_text(encoding="utf-8"), TARGET.read_text(encoding="utf-8")
    )

    for note in notes:
        print(f"check-env: {note}")

    if failures:
        for failure in failures:
            print(f"check-env: {failure}", file=sys.stderr)
        return 1

    print("check-env: .env matches the template, no sentinels left, Fernet key is well formed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
