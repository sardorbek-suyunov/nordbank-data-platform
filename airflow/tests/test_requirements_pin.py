"""DuckDB is pinned once, in the `warehouse` group, and the image's files come from the lock."""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IMAGE = ROOT / "infra" / "docker" / "airflow"


def _declarations() -> list[str]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    out = list(project["project"].get("dependencies", []))
    for entries in project["dependency-groups"].values():
        out.extend(entry for entry in entries if isinstance(entry, str))
    return out


def test_duckdb_is_declared_exactly_once_and_pinned():
    declarations = _declarations()
    assert len(declarations) >= 10
    duckdb = [d for d in declarations if re.match(r"duckdb\b", d)]
    assert len(duckdb) == 1 and duckdb[0].startswith("duckdb==")


def test_the_hand_written_image_requirements_do_not_pin_duckdb_again():
    lines = (IMAGE / "requirements.txt").read_text(encoding="utf-8").splitlines()
    assert len([line for line in lines if line and not line.startswith("#")]) >= 5
    assert not [line for line in lines if re.match(r"duckdb\b", line)]


def test_the_exports_carry_the_lock_pin_with_hashes():
    pin = next(d for d in _declarations() if re.match(r"duckdb\b", d))
    for name in ("requirements-warehouse.txt", "requirements-dbt.txt"):
        text = (IMAGE / name).read_text(encoding="utf-8")
        assert text.startswith("# Generated from uv.lock")
        assert f"{pin} \\" in text and "--hash=sha256:" in text
