"""Report how each contract differs from the current data dictionary.

The dictionary records what the source **is**; a contract records what the platform has
**agreed to accept**. They are bootstrapped equal and are expected to diverge, so this reports
by default and exits zero. `--check` makes it fail, and the `docs` CI job runs that form, so a
contract cannot drift from the dictionary unnoticed inside a commit. It is not a fifth required
check: the four in `docs/conventions.md` are unchanged.

No contract declares an expectation stricter than the source, so there is one kind of finding
here rather than two. A contract that narrowed what the source permits was considered at
version 2 of spec 005 and rejected, because a record-level rejection for a soft field
expectation produces permanent population loss rather than a quality signal; the reasoning is
in `docs/architecture.md` with the bronze contract.

**A contract that has accepted a scripted drift event is not divergent.** The dictionary
records the source's committed shape and a tick changes the live source without editing it,
which is why `make schema-check` compares against the dictionary *plus the deltas of the
events that have fired*. The same problem arrives here one step later: once a breaking drift
is resolved by bumping a contract, that contract permanently disagrees with the committed
dictionary, and the disagreement is the correct state rather than a defect. So a divergence
that matches a declared event's delta is reported as accepted drift and does not fail. Any
other divergence does.

This reads the timeline rather than the drift log, so it needs no database and still runs on a
clean checkout with nothing started. The weaker question it answers as a result — "is this one
of the shapes the source can legitimately take" rather than "is this the shape it has right
now" — is the right one for a committed file: a contract is reviewed in a pull request, where
no database is in scope.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from data_contract import (  # noqa: E402
    SOURCE_SYSTEM,
    dictionary_revision,
    load_all,
    load_history,
)
from schema_contract import read_dictionary  # noqa: E402

sys.path.insert(0, str(ROOT))
from generator import drift  # noqa: E402
from generator.drift import timeline  # noqa: E402

DICTIONARY = ROOT / "docs" / "data_dictionary.md"
CONTRACTS = ROOT / "contracts" / SOURCE_SYSTEM
EXTRACTED_SCHEMAS: tuple[str, ...] = ("core", "ref")


def accepted_drift(contract) -> dict[str, tuple[str, str]]:
    """Per column, the shape a declared drift event would give it, and the event's name.

    An additive event contributes the column it adds; a widening contributes the type it
    widens to. A contract carrying either has accepted that event.
    """
    out: dict[str, tuple[str, str]] = {}
    for event in drift.events():
        if event.target_schema != contract.source_schema or event.target_table != contract.entity:
            continue
        if event.drift_type == timeline.COLUMN_ADDED and event.added is not None:
            out[event.target_column] = (event.added.data_type, event.name)
        elif event.drift_type == timeline.TYPE_WIDENED:
            out[event.target_column] = (event.widened_to, event.name)
    return out


def differences(contract, documented: dict[str, object]) -> tuple[list[str], list[str]]:
    """Divergences that are findings, and divergences that are accepted drift."""
    found: list[str] = []
    accepted: list[str] = []
    drifted = accepted_drift(contract)
    contracted = {column.name: column for column in contract.columns}

    for name in sorted(set(contracted) | set(documented)):
        held = contracted.get(name)
        described = documented.get(name)
        if held is None:
            found.append(f"column {name} is in the dictionary and not in the contract")
            continue
        if described is None:
            expected = drifted.get(name)
            if expected and expected[0] == held.data_type:
                accepted.append(f"column {name} accepts drift event {expected[1]}")
            else:
                found.append(f"column {name} is in the contract and not in the dictionary")
            continue
        if held.data_type != described.data_type:
            expected = drifted.get(name)
            if expected and expected[0] == held.data_type:
                accepted.append(
                    f"column {name} accepts drift event {expected[1]}: "
                    f"{described.data_type} widened to {held.data_type}"
                )
            else:
                found.append(
                    f"column {name} type {held.data_type!r} against dictionary "
                    f"{described.data_type!r}"
                )
        if held.is_nullable != described.is_nullable:
            found.append(
                f"column {name} nullable {held.is_nullable} against dictionary "
                f"{described.is_nullable}"
            )
        if held.classification != described.classification:
            found.append(
                f"column {name} classification {held.classification!r} against dictionary "
                f"{described.classification!r}"
            )
    return found, accepted


def main(argv: list[str]) -> int:
    check = "--check" in argv

    documented = [c for c in read_dictionary(DICTIONARY) if c.schema in EXTRACTED_SCHEMAS]
    by_entity: dict[tuple[str, str], dict[str, object]] = {}
    for column in documented:
        by_entity.setdefault((column.schema, column.table), {})[column.name] = column

    contracts = load_all(CONTRACTS)
    # Only the current version is compared with the dictionary; a superseded one describes
    # the source as it was and would diverge for ever. The chain is still loaded, so a
    # broken one fails here and in CI rather than at the first replay that selects from it.
    chains = load_history(CONTRACTS)
    superseded = sum(len(versions) - 1 for versions in chains.values())
    revision = dictionary_revision(DICTIONARY)

    diverged = 0
    accepted_count = 0
    stale_pins = 0
    missing = []

    for key in sorted(by_entity):
        schema, table = key
        if table not in contracts:
            missing.append(f"{schema}.{table}")

    for entity in sorted(contracts):
        contract = contracts[entity]
        key = (contract.source_schema, contract.entity)
        if key not in by_entity:
            print(f"  {contract.source_schema}.{entity}: not in the dictionary at all")
            diverged += 1
            continue
        found, accepted = differences(contract, by_entity[key])
        if contract.dictionary_revision != revision:
            stale_pins += 1
        if accepted:
            accepted_count += 1
            print(
                f"  {contract.source_schema}.{entity}: contract version "
                f"{contract.contract_version} has accepted drift"
            )
            for line in accepted:
                print(f"    {line}")
        if found:
            diverged += 1
            pinned = contract.dictionary_revision
            moved = " (the dictionary has moved since)" if pinned != revision else ""
            print(f"  {contract.source_schema}.{entity}: pinned to {pinned}{moved}")
            for line in found:
                print(f"    {line}")

    for entity in missing:
        print(f"  {entity}: no contract")

    print(
        f"contracts-diff: {len(contracts)} contract(s), {len(by_entity)} documented entity(ies), "
        f"{diverged} diverging, {accepted_count} carrying accepted drift, "
        f"{len(missing)} uncontracted, {superseded} superseded version(s) under history/, "
        f"dictionary at {revision}"
    )
    # Authored contracts for the external feeds have no dictionary to diverge from, so they
    # are loaded rather than compared: a malformed one, or a broken version chain, fails here.
    authored = 0
    for directory in sorted(p for p in (ROOT / "contracts").iterdir() if p.is_dir()):
        if directory.name == SOURCE_SYSTEM:
            continue
        chains = load_history(directory)
        authored += len(chains)
        for entity, versions in sorted(chains.items()):
            current = versions[-1]
            print(
                f"  {directory.name}.{entity}: authored {current.kind} contract, version "
                f"{current.contract_version}, against {current.source_of_truth['document']}"
            )
    print(f"contracts-diff: {authored} authored contract(s) loaded")
    if authored < 5:
        print("contracts-diff: fewer than the five authored feed contracts were found")
        return 1

    if stale_pins:
        print(
            f"contracts-diff: {stale_pins} contract(s) pinned to an earlier dictionary revision; "
            "that is expected once the dictionary moves and is not itself a divergence"
        )

    if check and (diverged or missing):
        print("contracts-diff: --check fails on divergence", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
