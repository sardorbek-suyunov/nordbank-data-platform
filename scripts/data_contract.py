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

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

CONTRACT_ROOT = "contracts"
SOURCE_SYSTEM = "corebank"

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
    "columns",
)

CLASSIFICATIONS: frozenset[str] = frozenset(
    {"identifier", "quasi-identifier", "pseudonymous_key", "sensitive", "non-personal"}
)


class ContractError(Exception):
    """A malformed or missing contract. Carries the file it came from."""


@dataclass(frozen=True)
class ContractColumn:
    name: str
    data_type: str
    is_nullable: bool
    classification: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.data_type,
            "nullable": self.is_nullable,
            "classification": self.classification,
        }


@dataclass(frozen=True)
class Contract:
    source_system: str
    source_schema: str
    entity: str
    contract_version: int
    watermark_column: str
    primary_key: str
    dictionary_revision: str
    columns: tuple[ContractColumn, ...]

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
        return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_system": self.source_system,
            "source_schema": self.source_schema,
            "entity": self.entity,
            "contract_version": self.contract_version,
            "watermark_column": self.watermark_column,
            "primary_key": self.primary_key,
            "dictionary_revision": self.dictionary_revision,
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

    missing = [field for field in REQUIRED_FIELDS if field not in payload]
    if missing:
        raise ContractError(f"{source}: missing required field(s): {', '.join(missing)}")

    raw_columns = payload["columns"]
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
        columns.append(
            ContractColumn(
                name=raw["name"],
                data_type=raw["type"],
                is_nullable=raw["nullable"],
                classification=raw["classification"],
            )
        )

    names = [column.name for column in columns]
    if len(set(names)) != len(names):
        raise ContractError(f"{source}: duplicate column name(s)")

    contract = Contract(
        source_system=payload["source_system"],
        source_schema=payload["source_schema"],
        entity=payload["entity"],
        contract_version=int(payload["contract_version"]),
        watermark_column=payload["watermark_column"],
        primary_key=payload["primary_key"],
        dictionary_revision=payload["dictionary_revision"],
        columns=tuple(columns),
    )

    for required in (contract.primary_key, contract.watermark_column):
        if contract.column(required) is None:
            raise ContractError(f"{source}: column {required!r} is declared but not described")
    return contract


def load(path: Path) -> Contract:
    return parse(yaml.safe_load(path.read_text(encoding="utf-8")), path.as_posix())


def load_all(directory: Path) -> dict[str, Contract]:
    """Every contract under a directory, keyed on entity.

    The entity name is the key because it is what the object key and the bronze model name are
    built from. Two entities with the same name in different source schemas would collide
    there, so the collision is refused here, where it is one error message, rather than in the
    lake, where it is two sources writing one prefix.
    """
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
        "# commit, when the platform decides to accept something new.\n"
    )
    body = yaml.safe_dump(contract.as_dict(), sort_keys=False, default_flow_style=False, width=100)
    return header + body
