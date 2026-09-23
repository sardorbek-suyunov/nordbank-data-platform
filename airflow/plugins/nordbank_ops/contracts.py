"""Contract-of-the-time: which contract version a batch is validated against (spec 006 section 5).

Specification 005 validated every batch against the contract on disk, so replaying a day from
before a version bump meant checking out the old contract by hand. Here the version is chosen
by the batch's interval: the open step records every version on disk in `meta.contract_version`
with the business day it applies from, selects the one in force for the interval from there,
and stores its number on the batch. Extract and register then load that version's body, so a
batch is validated against one contract from open to register even if a bump lands mid-run.

The body stays on disk and the table holds its fingerprint. A version already recorded whose
file now has a different fingerprint, or a different `in_force_from`, is refused rather than
re-recorded: an old contract edited in place would change what a replay accepts, and nothing
else would notice.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

VersionChains = dict[str, tuple[Any, ...]]


class ContractSelectionError(RuntimeError):
    """A recorded version and its file disagree, or no version is in force."""


def load_chains(directory) -> VersionChains:
    from data_contract import load_history

    return load_history(directory)


def sync(connection: Any, chains: VersionChains, now: dt.datetime) -> int:
    """Record every version on disk, and refuse one whose file changed after it was recorded."""
    added = 0
    for entity in sorted(chains):
        for contract in chains[entity]:
            row = connection.execute(
                """
                select contract_fingerprint, in_force_from
                  from meta.contract_version
                 where source_system = ? and entity = ? and contract_version = ?
                """,
                [contract.source_system, entity, contract.contract_version],
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    insert into meta.contract_version (
                        source_system, entity, contract_version, dictionary_revision,
                        contract_fingerprint, in_force_from, first_seen_at
                    ) values (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        contract.source_system,
                        entity,
                        contract.contract_version,
                        contract.dictionary_revision,
                        contract.fingerprint,
                        contract.in_force_from,
                        now,
                    ],
                )
                added += 1
                continue
            fingerprint, in_force_from = row
            if fingerprint != contract.fingerprint or in_force_from != contract.in_force_from:
                raise ContractSelectionError(
                    f"{contract.source_system}.{entity} version {contract.contract_version} was "
                    f"recorded as {fingerprint} in force from {in_force_from}, and its file now "
                    f"says {contract.fingerprint} from {contract.in_force_from}. A recorded "
                    "version is not edited in place: describe the change as a new version."
                )
    return added


def select(connection: Any, source_system: str, entity: str, day: dt.date) -> int:
    """The version in force for a batch whose interval starts on `day`, from the table."""
    row = connection.execute(
        """
        select max(contract_version)
          from meta.contract_version
         where source_system = ? and entity = ?
           and (in_force_from is null or in_force_from <= ?)
        """,
        [source_system, entity, day],
    ).fetchone()
    if row is None or row[0] is None:
        raise ContractSelectionError(
            f"{source_system}.{entity}: no contract version is in force on {day.isoformat()}"
        )
    return int(row[0])


def body(chains: VersionChains, entity: str, version: int):
    """The contract of one version, from disk."""
    for contract in chains.get(entity, ()):
        if contract.contract_version == version:
            return contract
    raise ContractSelectionError(
        f"{entity} version {version} is recorded as in force and has no file on disk; a "
        "superseded version belongs under history/ and is never deleted"
    )
