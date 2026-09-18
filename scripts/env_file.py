"""Read .env for the helper scripts, the same way Compose does.

Values are returned, never exported. A value exported into this process would be inherited by
`docker compose`, which prefers the shell environment over the file, so one parsing mistake
here would silently override what the stack actually runs with.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def strip_inline_comment(value: str) -> str:
    """Compose treats ` #` as the start of a comment; a bare `#` inside a value is kept."""
    marker = value.find(" #")
    return (value[:marker] if marker != -1 else value).strip()


def read_dotenv(path: Path | None = None) -> dict[str, str]:
    target = path or ROOT / ".env"
    values: dict[str, str] = {}
    if not target.exists():
        return values

    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = strip_inline_comment(value)
    return values
