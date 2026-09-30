"""Scan the documentation site before it is published (spec 007 section 9).

`dbt docs generate --static` writes one HTML page carrying the whole manifest and catalogue:
every model's compiled SQL, every description, every column. The site is public, so before it
is uploaded every file of the artefact is searched for:

- **every secret the environment holds**: each generated or external value in `.env`, found by
  laying the template `.env.example` over it, so a secret inside a connection URI or JSON is
  found as the generated part and not only as the whole string;
- **every vault value**, with the PII scan's matcher and encodings (`bronze_pii_scan.py`): the
  raw identifier values, in every byte form a JSON document or an HTML page can hold them.

A hit names the variable or the token and the file, never the value, and fails the scan. `--plant`
proves it can fail: it copies the artefact, writes one secret and one vault value into the copy,
and requires both caught while the original stays clean.

It also checks what the site is published for: that it shows every column's classification. The
generated properties declare one per column, and the site must carry at least that many.

Runs inside the scheduler, where the vault is; the caller copies `.env` and `.env.example` in
beside the artefact and removes them afterwards.

Usage: `python scripts/docs_scan.py <artefact dir> <.env> <.env.example> [--plant]`.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import tempfile
from pathlib import Path

for _path in ("/opt/airflow/plugins", "/opt/airflow/scripts"):
    if _path not in sys.path:
        sys.path.insert(0, _path)

SENTINELS = ("__GENERATE__", "__EXTERNAL__")
SHORTEST_SECRET = 8
PROPERTIES = Path("/opt/airflow/dbt/models/bronze")


def _values(text: str) -> dict[str, str]:
    from env_file import strip_inline_comment

    out = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        out[name.strip()] = strip_inline_comment(value).strip()
    return out


def secrets(env_text: str, template_text: str) -> dict[str, str]:
    """name -> secret value: the part of each value the template marks generated or external."""
    env, template = _values(env_text), _values(template_text)
    out = {}
    for name, pattern in template.items():
        sentinel = next((s for s in SENTINELS if s in pattern), None)
        value = env.get(name, "")
        if sentinel is None or not value or value in SENTINELS:
            continue
        prefix, suffix = pattern.split(sentinel, 1)
        if value.startswith(prefix) and value.endswith(suffix):
            value = value[len(prefix) : len(value) - len(suffix)]
        if len(value) >= SHORTEST_SECRET:
            out[name] = value
    return out


def vault_matcher():
    from bronze_pii_scan import Matcher, encodings, vault_values
    from nordbank_ops import warehouse

    with warehouse.connect(read_only=True) as connection:
        values = vault_values(connection)
    return Matcher([(token, encodings(raw)) for token, raw in values]), len(values)


def scan(directory: Path, named_secrets: dict[str, str], matcher) -> list[str]:
    hits = []
    files = [p for p in directory.rglob("*") if p.is_file()]
    for path in files:
        body = path.read_bytes()
        for name, value in named_secrets.items():
            if value.encode("utf-8") in body:
                hits.append(f"{path.name}: the value of {name}")
        for token, length in matcher.tokens_in(body).items():
            hits.append(f"{path.name}: vault value of token {token} ({length} bytes)")
    return hits


def classifications(directory: Path) -> tuple[int, int]:
    """(columns the properties classify, classifications the site shows)."""
    declared = sum(
        text.count("            classification: ")
        for text in (p.read_text(encoding="utf-8") for p in PROPERTIES.glob("br_*.yml"))
    )
    shown = sum(
        len(re.findall(rb"Classification: `[a-z_-]+`", p.read_bytes()))
        for p in directory.rglob("*.html")
    )
    return declared, shown


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("artefact", type=Path)
    parser.add_argument("env", type=Path)
    parser.add_argument("template", type=Path)
    parser.add_argument("--plant", action="store_true")
    arguments = parser.parse_args(argv)

    named = secrets(
        arguments.env.read_text(encoding="utf-8"), arguments.template.read_text(encoding="utf-8")
    )
    matcher, vaulted = vault_matcher()
    files = [p for p in arguments.artefact.rglob("*") if p.is_file()]
    print(
        f"docs-scan: {len(files)} file(s), {sum(p.stat().st_size for p in files)} bytes, "
        f"against {len(named)} secret(s) and {vaulted} vault value(s)"
    )
    # A scan with nothing to look for, or nothing to look in, proves nothing.
    if len(named) < 5 or not files:
        print("docs-scan: fewer than five secrets or no file to scan; not a clean result")
        return 1

    if arguments.plant:
        return plant(arguments.artefact, named, matcher)

    hits = scan(arguments.artefact, named, matcher)
    for hit in hits:
        print(f"docs-scan: FOUND {hit}")
    declared, shown = classifications(arguments.artefact)
    print(f"docs-scan: the site shows {shown} classification(s); the properties declare {declared}")
    if declared < 400 or shown < declared:
        print("docs-scan: the site does not show every column's classification")
        return 1
    print(f"docs-scan: {'no secret and no vault value found' if not hits else 'NOT CLEAN'}")
    return 1 if hits else 0


def plant(artefact: Path, named: dict[str, str], matcher) -> int:
    from nordbank_ops import warehouse

    with warehouse.connect(read_only=True) as connection:
        (raw,) = connection.execute(
            "select raw_value from meta.pii_vault order by token limit 1"
        ).fetchone()
    name, value = sorted(named.items())[0]
    with tempfile.TemporaryDirectory() as scratch:
        copy = Path(scratch) / "site"
        shutil.copytree(artefact, copy)
        page = next(copy.rglob("*.html"))
        page.write_bytes(page.read_bytes() + f"<!-- {value} {raw} -->".encode())
        hits = scan(copy, named, matcher)
    secret_caught = any(name in hit for hit in hits)
    vault_caught = any("vault value" in hit for hit in hits)
    clean = not scan(artefact, named, matcher)
    original = "stays clean" if clean else "IS NOT CLEAN"
    print(
        f"docs-scan: planted a secret ({name}) and a vault value in a copy of the site: "
        f"secret {'caught' if secret_caught else 'MISSED'}, vault value "
        f"{'caught' if vault_caught else 'MISSED'}; the original {original}"
    )
    return 0 if secret_caught and vault_caught and clean else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
