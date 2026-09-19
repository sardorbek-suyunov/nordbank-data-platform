"""The run manifest: what was generated, from what, and a digest of every table.

The manifest is what makes determinism an enforced invariant rather than a claim. The `ci`
manifest is committed, and CI regenerates it and fails on any difference, so a change that
alters generated values cannot land without someone noticing and saying why.

**The digest is an additive one, not a hash of the whole table.** The obvious
`md5(string_agg(md5(row), ''))` has to materialise every row hash in memory: at the full
profile that is a 1.3 GB string for `core.transactions` alone. This sums two independent 32-bit
slices of each row's md5 instead, which streams in constant memory at any scale and is
order-independent, and reports the row count beside them. It is chosen to detect a generator
that changed, not to resist an adversary constructing a collision, and that is the property the
committed manifest needs.

Text rendering is pinned before the digest is taken. `row::text` renders a `timestamptz` in the
session's time zone, so a manifest taken in one time zone would not match the same data digested
in another.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import GENERATOR_VERSION
from .config import RunConfig
from .tables import LOAD_ORDER, TABLE_COLUMNS

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import source_db_exec as db  # noqa: E402

MANIFEST_DIR = Path(__file__).resolve().parent / "manifests"

# Pinned so that the text a row renders as cannot depend on the session that asked.
SESSION_SETTINGS = (
    "set timezone = 'UTC';\nset datestyle = 'ISO, YMD';\nset extra_float_digits = 3;\n"
)


@dataclass
class TableDigest:
    rows: int
    digest: str


@dataclass
class Manifest:
    profile: str
    seed: int
    anchor: str
    generator_version: str
    tables: dict[str, TableDigest] = field(default_factory=dict)
    total_rows: int = 0
    runtime_seconds: float | None = None

    def to_json(self, *, with_runtime: bool) -> str:
        payload = {
            "profile": self.profile,
            "seed": self.seed,
            "anchor": self.anchor,
            "generator_version": self.generator_version,
            "total_rows": self.total_rows,
            "tables": {name: asdict(digest) for name, digest in sorted(self.tables.items())},
        }
        if with_runtime and self.runtime_seconds is not None:
            payload["runtime_seconds"] = round(self.runtime_seconds, 2)
        return json.dumps(payload, indent=2, sort_keys=False) + "\n"

    def comparable(self) -> dict:
        """The part a committed manifest is compared on.

        Runtime is excluded deliberately: it is worth recording and it differs between machines,
        so comparing on it would make the determinism check fail for a reason that has nothing to
        do with determinism.
        """
        return json.loads(self.to_json(with_runtime=False))


# Field separator and null marker inside a row's digest input. Unit separator and record
# separator: neither can appear in a value, so no combination of values can be confused with
# another by their concatenation.
FIELD = "chr(31)"
NULL_MARKER = "chr(30)"


def _row_expression(table: str) -> str:
    """The text a row is digested from: its documented columns, in documented order.

    Explicitly rather than `row::text`, which renders columns in *physical* order. The two
    differ whenever a column was added by `ALTER TABLE` rather than being present when the
    table was created, because an added column goes on the end. A database built by applying
    the DDL to an existing stack and one built from scratch then hold the same data in a
    different physical order, and a digest over `row::text` reports them as different.

    That is not hypothetical: it is what the committed manifest caught between a developer
    machine that had evolved through the M2 schema changes and a CI runner starting clean, on
    `core.transactions` and `core.gl_transactions` — the two tables that gained columns. The
    row counts matched and every value matched; only the order they were rendered in did not.

    `coalesce` to a marker rather than letting a NULL swallow the field, so that (a, NULL, b)
    and (a, b, NULL) cannot digest alike.
    """
    fields = ", ".join(
        f"coalesce({column}::text, {NULL_MARKER})" for column in TABLE_COLUMNS[table]
    )
    return f"md5(concat_ws({FIELD}, {fields}))"


def _digest_sql() -> str:
    parts = []
    for table in LOAD_ORDER:
        parts.append(f"\\echo @@digest@@{table}")
        parts.append(
            f"""
            select count(*)::text || ':' ||
                   coalesce(sum(('x' || substr(h, 1, 8))::bit(32)::bigint), 0)::text || ':' ||
                   coalesce(sum(('x' || substr(h, 9, 8))::bit(32)::bigint), 0)::text
              from (select {_row_expression(table)} as h from core.{table}) s;
            """  # noqa: S608
        )
    return SESSION_SETTINGS + "\n".join(parts) + "\n"


def build(config: RunConfig, runtime_seconds: float | None = None) -> Manifest:
    command = db._psql_command(True, ["-A", "-t", "-F", db.FIELD_SEPARATOR])
    completed = db._run(command, stdin=_digest_sql())
    if completed.returncode != 0:
        raise RuntimeError(
            "manifest could not be built:\n" + (completed.stdout + completed.stderr).strip()[:2000]
        )

    manifest = Manifest(
        profile=config.profile.name,
        seed=config.seed,
        anchor=config.anchor.isoformat(),
        generator_version=GENERATOR_VERSION,
        runtime_seconds=runtime_seconds,
    )
    current: str | None = None
    for line in completed.stdout.splitlines():
        if line.startswith("@@digest@@"):
            current = line[len("@@digest@@") :].strip()
        elif current and line.strip():
            rows_text, first, second = line.strip().split(":")
            manifest.tables[current] = TableDigest(
                rows=int(rows_text), digest=f"{int(first):d}:{int(second):d}"
            )
            current = None
    manifest.total_rows = sum(digest.rows for digest in manifest.tables.values())
    return manifest


def path_for(profile_name: str) -> Path:
    return MANIFEST_DIR / f"{profile_name}.json"


def write(manifest: Manifest) -> Path:
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    target = path_for(manifest.profile)
    target.write_text(manifest.to_json(with_runtime=True), encoding="utf-8")
    return target


def compare(manifest: Manifest) -> list[str]:
    """Differences between a freshly built manifest and the committed one, table by table."""
    target = path_for(manifest.profile)
    if not target.exists():
        return [f"no committed manifest at {target.relative_to(ROOT).as_posix()}"]

    committed = json.loads(target.read_text(encoding="utf-8"))
    fresh = manifest.comparable()
    differences: list[str] = []

    for key in ("profile", "seed", "anchor", "generator_version", "total_rows"):
        if committed.get(key) != fresh.get(key):
            differences.append(f"{key}: committed {committed.get(key)!r}, fresh {fresh.get(key)!r}")

    committed_tables = committed.get("tables", {})
    fresh_tables = fresh["tables"]
    for table in sorted(set(committed_tables) | set(fresh_tables)):
        left = committed_tables.get(table)
        right = fresh_tables.get(table)
        if left is None:
            differences.append(f"{table}: absent from the committed manifest")
        elif right is None:
            differences.append(f"{table}: absent from the fresh manifest")
        elif left != right:
            differences.append(
                f"{table}: committed {left['rows']:,} rows / {left['digest']}, "
                f"fresh {right['rows']:,} rows / {right['digest']}"
            )
    return differences


def format_manifest(manifest: Manifest) -> str:
    lines = [
        f"seed-manifest: profile {manifest.profile}, seed {manifest.seed}, "
        f"anchor {manifest.anchor}, generator {manifest.generator_version}",
        "",
        f"  {'table':<22} {'rows':>12}  digest",
        f"  {'-' * 22} {'-' * 12}  {'-' * 24}",
    ]
    for name, digest in sorted(manifest.tables.items()):
        lines.append(f"  {name:<22} {digest.rows:>12,}  {digest.digest}")
    lines.append(f"  {'-' * 22} {'-' * 12}")
    lines.append(f"  {'total':<22} {manifest.total_rows:>12,}")
    if manifest.runtime_seconds is not None:
        lines.append("")
        lines.append(f"  runtime {manifest.runtime_seconds:.2f} s")
    return "\n".join(lines)


def anchor_default() -> dt.date:
    return dt.date.today()
