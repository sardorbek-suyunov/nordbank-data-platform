"""The data contract: what the platform has agreed to accept from a source entity.

This is not the same object as `schema_contract.py`, and the difference is the whole mechanism
spec 005 section 2 describes. `docs/data_dictionary.md` records what the source **is**, and
`make schema-check` proves the live database matches it. A contract records what the platform
has **agreed to accept**. They start equal, because the contract is bootstrapped from the
dictionary, and the lag between them afterwards is what makes drift visible at ingest.

Those are two facts, not one fact in two places. A reader who believes otherwise will delete a
copy and remove the control.

The model lives here rather than in `airflow/plugins/nordbank_ops/` because it is the same kind
of object as the dictionary parser next to it, and the bootstrap and the diff read both
together. The runtime that acts on a contract — validation, tokenisation, drift classification,
the registry — is orchestration code and lives in the plugin tree.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

CONTRACT_ROOT = "contracts"
SOURCE_SYSTEM = "corebank"

# Superseded contract versions, one file each, named `<entity>.v<N>.yml`. They live in a
# subdirectory so that `load_all`, which globs the source directory itself, keeps returning
# exactly one contract per entity, the current one, and `contracts-diff` keeps comparing only
# that against the dictionary. A superseded version compared against today's dictionary would
# report permanent divergence and fail CI for ever.
HISTORY_DIR = "history"

# The fields a contract file must carry. A missing one is an error rather than a default,
# because every default here would be the permissive answer.
REQUIRED_FIELDS: tuple[str, ...] = (
    "source_system",
    "source_schema",
    "entity",
    "contract_version",
    "watermark_column",
    "primary_key",
    "dictionary_revision",
    "in_force_from",
    "columns",
)

CLASSIFICATIONS: frozenset[str] = frozenset(
    {"identifier", "quasi-identifier", "pseudonymous_key", "sensitive", "non-personal"}
)

# A contract for a third party's file, API or snapshot is authored rather than bootstrapped
# (spec 006 section 5): there is no dictionary entry for someone else's format, so it names its
# source of truth instead, describes the format's shape, and has no watermark. A contract with
# no `kind` is the relational shape specification 005 defined, and is held to all of that
# shape's fields: the absence of a kind is not a permissive default.
RELATIONAL = "relational"
AUTHORED_KINDS: frozenset[str] = frozenset({"file", "api", "snapshot"})
AUTHORED_FIELDS: tuple[str, ...] = (
    "source_system",
    "source_schema",
    "entity",
    "kind",
    "contract_version",
    "in_force_from",
    "source_of_truth",
    "primary_key",
    "format",
    "columns",
)

# Where an authored contract's column comes from. `record` is a field of the record itself;
# `header` is a field of the delivery's header that every record inherits, such as a settlement
# file's settlement date or a snapshot's version; `request` is what was asked for, such as a
# FRED series id. Only `record` columns take part in the format's shape check.
ORIGINS: frozenset[str] = frozenset({"record", "header", "request"})


class ContractError(Exception):
    """A malformed or missing contract. Carries the file it came from."""


@dataclass(frozen=True)
class ContractColumn:
    name: str
    data_type: str
    is_nullable: bool
    classification: str
    origin: str = "record"

    def as_dict(self) -> dict[str, Any]:
        out = {
            "name": self.name,
            "type": self.data_type,
            "nullable": self.is_nullable,
            "classification": self.classification,
        }
        if self.origin != "record":
            out["origin"] = self.origin
        return out


@dataclass(frozen=True)
class Contract:
    source_system: str
    source_schema: str
    entity: str
    contract_version: int
    watermark_column: str | None
    primary_key: str
    dictionary_revision: str | None
    columns: tuple[ContractColumn, ...]
    # Source time: the first business day this version applies to. `None` means from the start
    # of the source's history, which only a version 1 may say. It is authored, and it is not
    # the real-time moment the platform first saw the version, which `meta.contract_version`
    # records separately as `first_seen_at`: the two answer different questions.
    in_force_from: dt.date | None = None
    kind: str = RELATIONAL
    # Authored contracts only. The source of truth is named in the contract itself, and the
    # format describes the delivery's shape: a file's header records and field order, or an API
    # response's top-level keys.
    source_of_truth: dict | None = None
    format: dict | None = None
    key_columns: tuple[str, ...] = ()

    @property
    def is_authored(self) -> bool:
        return self.kind in AUTHORED_KINDS

    @property
    def keys(self) -> tuple[str, ...]:
        """The primary key's columns, one for a relational contract and one or more otherwise."""
        return self.key_columns or (self.primary_key,)

    @property
    def record_columns(self) -> tuple[str, ...]:
        """The columns that are fields of the record, in the order the format delivers them."""
        return tuple(c.name for c in self.columns if c.origin == "record")

    @property
    def qualified_relation(self) -> str:
        """What `_source_file` carries for a relational source."""
        return f"{self.source_schema}.{self.entity}"

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)

    def column(self, name: str) -> ContractColumn | None:
        for candidate in self.columns:
            if candidate.name == name:
                return candidate
        return None

    def identifier_columns(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns if c.classification == "identifier")

    @property
    def fingerprint(self) -> str:
        """A hash of everything the contract asserts, for `meta.contract_version`.

        Two contracts with the same version and different content is the failure this exists to
        make visible: the version is a claim and the fingerprint is the evidence.
        """
        material = "|".join(
            f"{c.name}:{c.data_type}:{int(c.is_nullable)}:{c.classification}" for c in self.columns
        )
        material = f"{self.source_schema}.{self.entity}:{self.primary_key}:{material}"
        if self.is_authored:
            import json

            shape = json.dumps(
                {
                    "kind": self.kind,
                    "keys": list(self.keys),
                    "origins": [c.origin for c in self.columns],
                    "format": self.format,
                },
                sort_keys=True,
                default=str,
            )
            material = f"{material}|{shape}"
        return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]

    def as_dict(self) -> dict[str, Any]:
        if self.is_authored:
            return {
                "source_system": self.source_system,
                "source_schema": self.source_schema,
                "entity": self.entity,
                "kind": self.kind,
                "contract_version": self.contract_version,
                "in_force_from": self.in_force_from,
                "source_of_truth": self.source_of_truth,
                "primary_key": list(self.keys),
                "format": self.format,
                "columns": [column.as_dict() for column in self.columns],
            }
        return {
            "source_system": self.source_system,
            "source_schema": self.source_schema,
            "entity": self.entity,
            "contract_version": self.contract_version,
            "watermark_column": self.watermark_column,
            "primary_key": self.primary_key,
            "dictionary_revision": self.dictionary_revision,
            "in_force_from": self.in_force_from,
            "columns": [column.as_dict() for column in self.columns],
        }


def dictionary_revision(path: Path) -> str:
    """The dictionary's identity, as a content hash rather than a git revision.

    A git revision would be empty for an uncommitted dictionary and unavailable inside the
    container, and what the diff actually needs is "did this file change", which a hash answers
    exactly.

    Line endings are normalised before hashing. `.gitattributes` sets `eol=lf` so a checkout is
    LF everywhere, but a file written by a tool on Windows is not, and a revision that moved
    with the platform would report every contract as pinned to an earlier dictionary on a host
    that had never edited it.
    """
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"sha256:{digest[:16]}"


def parse(payload: Any, source: str) -> Contract:
    if not isinstance(payload, dict):
        raise ContractError(f"{source}: expected a mapping at the top level")
    if "kind" in payload:
        return _parse_authored(payload, source)

    missing = [field for field in REQUIRED_FIELDS if field not in payload]
    if missing:
        raise ContractError(f"{source}: missing required field(s): {', '.join(missing)}")

    columns = _parse_columns(payload["columns"], source, allow_origin=False)

    names = [column.name for column in columns]
    if len(set(names)) != len(names):
        raise ContractError(f"{source}: duplicate column name(s)")

    in_force_from = _in_force_from(payload["in_force_from"], source)
    version = int(payload["contract_version"])
    if in_force_from is None and version != 1:
        raise ContractError(
            f"{source}: version {version} has no in_force_from; only a version 1 applies from "
            "the start of history, and every later version must say which business day it "
            "takes over from"
        )

    contract = Contract(
        source_system=payload["source_system"],
        source_schema=payload["source_schema"],
        entity=payload["entity"],
        contract_version=version,
        watermark_column=payload["watermark_column"],
        primary_key=payload["primary_key"],
        dictionary_revision=payload["dictionary_revision"],
        columns=tuple(columns),
        in_force_from=in_force_from,
    )

    for required in (contract.primary_key, contract.watermark_column):
        if contract.column(required) is None:
            raise ContractError(f"{source}: column {required!r} is declared but not described")
    return contract


def _parse_columns(raw_columns: Any, source: str, *, allow_origin: bool) -> list[ContractColumn]:
    if not isinstance(raw_columns, list) or not raw_columns:
        raise ContractError(f"{source}: columns must be a non-empty list")
    columns = []
    for index, raw in enumerate(raw_columns):
        if not isinstance(raw, dict):
            raise ContractError(f"{source}: column {index} is not a mapping")
        for field in ("name", "type", "nullable", "classification"):
            if field not in raw:
                raise ContractError(f"{source}: column {index} is missing '{field}'")
        if raw["classification"] not in CLASSIFICATIONS:
            raise ContractError(
                f"{source}: column {raw['name']} has classification "
                f"{raw['classification']!r}, which is not one of "
                f"{', '.join(sorted(CLASSIFICATIONS))}"
            )
        if not isinstance(raw["nullable"], bool):
            raise ContractError(f"{source}: column {raw['name']} has a non-boolean 'nullable'")
        origin = raw.get("origin", "record")
        if origin != "record" and not allow_origin:
            raise ContractError(
                f"{source}: column {raw['name']} declares an origin, which only "
                "an authored contract can"
            )
        if origin not in ORIGINS:
            raise ContractError(
                f"{source}: column {raw['name']} has origin {origin!r}, which is not one of "
                f"{', '.join(sorted(ORIGINS))}"
            )
        columns.append(
            ContractColumn(
                name=raw["name"],
                data_type=raw["type"],
                is_nullable=raw["nullable"],
                classification=raw["classification"],
                origin=origin,
            )
        )
    return columns


def _parse_authored(payload: dict, source: str) -> Contract:
    """A contract for a third party's file, API or snapshot (spec 006 section 5)."""
    missing = [field for field in AUTHORED_FIELDS if field not in payload]
    if missing:
        raise ContractError(f"{source}: missing required field(s): {', '.join(missing)}")
    if payload["kind"] not in AUTHORED_KINDS:
        raise ContractError(
            f"{source}: kind {payload['kind']!r} is not one of {', '.join(sorted(AUTHORED_KINDS))}"
        )
    truth = payload["source_of_truth"]
    if not isinstance(truth, dict) or not truth.get("document"):
        raise ContractError(
            f"{source}: source_of_truth must name the document the contract is authored against"
        )
    if not isinstance(payload["format"], dict) or not payload["format"]:
        raise ContractError(f"{source}: format must describe the delivery's shape")

    raw_key = payload["primary_key"]
    keys = tuple(raw_key) if isinstance(raw_key, list) else (raw_key,)
    if not keys or not all(isinstance(k, str) and k for k in keys):
        raise ContractError(f"{source}: primary_key must name one or more columns")

    columns = _parse_columns(payload["columns"], source, allow_origin=True)
    names = [column.name for column in columns]
    if len(set(names)) != len(names):
        raise ContractError(f"{source}: duplicate column name(s)")

    in_force_from = _in_force_from(payload["in_force_from"], source)
    version = int(payload["contract_version"])
    if in_force_from is None and version != 1:
        raise ContractError(
            f"{source}: version {version} has no in_force_from; only a version 1 applies from "
            "the start of history, and every later version must say which business day it "
            "takes over from"
        )

    contract = Contract(
        source_system=payload["source_system"],
        source_schema=payload["source_schema"],
        entity=payload["entity"],
        contract_version=version,
        watermark_column=None,
        primary_key=", ".join(keys),
        dictionary_revision=None,
        columns=tuple(columns),
        in_force_from=in_force_from,
        kind=payload["kind"],
        source_of_truth=truth,
        format=payload["format"],
        key_columns=keys,
    )
    for key in keys:
        if contract.column(key) is None:
            raise ContractError(f"{source}: key column {key!r} is declared but not described")
    return contract


def _in_force_from(raw: Any, source: str) -> dt.date | None:
    if raw is None:
        return None
    if isinstance(raw, dt.datetime):
        raise ContractError(f"{source}: in_force_from is a business day, not a timestamp")
    if isinstance(raw, dt.date):
        return raw
    try:
        return dt.date.fromisoformat(str(raw))
    except ValueError as exc:
        raise ContractError(f"{source}: in_force_from {raw!r} is not a date") from exc


def load(path: Path) -> Contract:
    return parse(yaml.safe_load(path.read_text(encoding="utf-8")), path.as_posix())


def load_all(directory: Path) -> dict[str, Contract]:
    """Every contract under a directory, keyed on entity.

    The entity name is the key because it is what the object key and the bronze model name are
    built from. Two entities with the same name in different source schemas would collide
    there, so the collision is refused here, where it is one error message, rather than in the
    lake, where it is two sources writing one prefix.

    A missing directory raises rather than returning nothing. That is not defensive
    programming: it is the defect CI found on this milestone's pull request, where the DAG
    factory resolved the contract root to a path that exists only inside the image, loaded
    zero contracts on the runner, and built two ingestion DAGs with no entities, no assets and
    nothing to do — all of it green.
    """
    if not directory.is_dir():
        raise ContractError(
            f"{directory.as_posix()}: no such directory. A missing contract directory is a "
            "configuration failure and not an empty one: returning no contracts here would "
            "build an ingestion DAG with nothing to ingest, and it would succeed."
        )

    contracts: dict[str, Contract] = {}
    for path in sorted(directory.glob("*.yml")):
        contract = load(path)
        if contract.entity in contracts:
            other = contracts[contract.entity]
            raise ContractError(
                f"{path.as_posix()}: entity {contract.entity!r} is already contracted from "
                f"{other.source_schema}; entity names must be unique across source schemas "
                f"because the lake key and the bronze model name carry the entity alone"
            )
        contracts[contract.entity] = contract
    return contracts


def load_history(directory: Path) -> dict[str, tuple[Contract, ...]]:
    """Every version of every contract under a directory, oldest first, keyed on entity.

    The current version is the file in the directory itself; superseded ones are under
    `history/`. The chain is checked rather than trusted, because a replay selects from it and
    a broken chain would select silently: versions strictly increase, the current file is the
    highest, only the first may apply from the start of history, and each later version takes
    over on a strictly later day than the one before it.
    """
    current = load_all(directory)
    chains: dict[str, list[Contract]] = {entity: [c] for entity, c in current.items()}

    history = directory / HISTORY_DIR
    if history.is_dir():
        for path in sorted(history.glob("*.yml")):
            contract = load(path)
            entity, _, suffix = path.stem.rpartition(".v")
            if entity != contract.entity or suffix != str(contract.contract_version):
                raise ContractError(
                    f"{path.as_posix()}: a superseded contract is named "
                    f"<entity>.v<version>.yml, and this one holds {contract.entity} "
                    f"version {contract.contract_version}"
                )
            if entity not in chains:
                raise ContractError(
                    f"{path.as_posix()}: superseded version of {entity!r}, which has no "
                    "current contract"
                )
            chains[entity].append(contract)

    ordered: dict[str, tuple[Contract, ...]] = {}
    for entity, versions in chains.items():
        versions.sort(key=lambda c: c.contract_version)
        numbers = [c.contract_version for c in versions]
        where = f"{directory.as_posix()}: {entity}"
        if len(set(numbers)) != len(numbers):
            raise ContractError(f"{where}: version recorded twice in {numbers}")
        if versions[-1] is not current[entity]:
            raise ContractError(
                f"{where}: the current file is version {current[entity].contract_version} "
                f"and history holds version {numbers[-1]}; the current file must be the highest"
            )
        if len(versions) > 1 and versions[0].in_force_from is not None:
            raise ContractError(f"{where}: the oldest version must apply from the start")
        for earlier, later in zip(versions, versions[1:], strict=False):
            if later.in_force_from is None or (
                earlier.in_force_from is not None and later.in_force_from <= earlier.in_force_from
            ):
                raise ContractError(
                    f"{where}: version {later.contract_version} must take over on a later day "
                    f"than version {earlier.contract_version}"
                )
        ordered[entity] = tuple(versions)
    return ordered


def in_force(versions: tuple[Contract, ...], day: dt.date) -> Contract:
    """The version in force for a batch whose interval starts on `day`.

    The highest version whose `in_force_from` is on or before the day. A day before every
    version's start has no contract, and is refused rather than given the oldest one.
    """
    chosen = None
    for contract in versions:
        if contract.in_force_from is None or contract.in_force_from <= day:
            chosen = contract
    if chosen is None:
        entity = versions[0].entity if versions else "?"
        raise ContractError(f"{entity}: no contract version is in force on {day.isoformat()}")
    return chosen


def dump(contract: Contract) -> str:
    """Render a contract, with a header saying what it is and how it is meant to change."""
    header = (
        f"# Data contract for {contract.source_schema}.{contract.entity}.\n"
        "#\n"
        "# Bootstrapped from docs/data_dictionary.md once, then hand-authored. It is not\n"
        "# regenerated on every run: the dictionary records what the source is, this records\n"
        "# what the platform has agreed to accept, and the lag between them is what makes\n"
        "# drift visible at ingest (spec 005 section 2).\n"
        "#\n"
        "# `make contracts-diff` reports divergence from the dictionary and names the\n"
        "# revision this file was pinned to. Bump `contract_version` deliberately, in a\n"
        "# commit, when the platform decides to accept something new: move this file to\n"
        "# history/<entity>.v<N>.yml and give the new one the first business day it\n"
        "# applies to as `in_force_from`. `null` means from the start of history.\n"
    )
    body = yaml.safe_dump(contract.as_dict(), sort_keys=False, default_flow_style=False, width=100)
    return header + body
