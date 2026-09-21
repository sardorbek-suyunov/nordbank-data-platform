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

It reads no database. The dictionary and the contracts are both committed files, so this runs
on a clean checkout with nothing started.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from data_contract import SOURCE_SYSTEM, dictionary_revision, load_all  # noqa: E402
from schema_contract import read_dictionary  # noqa: E402

DICTIONARY = ROOT / "docs" / "data_dictionary.md"
CONTRACTS = ROOT / "contracts" / SOURCE_SYSTEM
EXTRACTED_SCHEMAS: tuple[str, ...] = ("core", "ref")


def differences(contract, documented: dict[str, object]) -> list[str]:
    found = []
    contracted = {column.name: column for column in contract.columns}

    for name in sorted(set(contracted) | set(documented)):
        held = contracted.get(name)
        described = documented.get(name)
        if held is None:
            found.append(f"column {name} is in the dictionary and not in the contract")
            continue
        if described is None:
            found.append(f"column {name} is in the contract and not in the dictionary")
            continue
        if held.data_type != described.data_type:
            found.append(
                f"column {name} type {held.data_type!r} against dictionary {described.data_type!r}"
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
    return found


def main(argv: list[str]) -> int:
    check = "--check" in argv

    documented = [c for c in read_dictionary(DICTIONARY) if c.schema in EXTRACTED_SCHEMAS]
    by_entity: dict[tuple[str, str], dict[str, object]] = {}
    for column in documented:
        by_entity.setdefault((column.schema, column.table), {})[column.name] = column

    contracts = load_all(CONTRACTS)
    revision = dictionary_revision(DICTIONARY)

    diverged = 0
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
        found = differences(contract, by_entity[key])
        if contract.dictionary_revision != revision:
            stale_pins += 1
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
        f"{diverged} diverging, {len(missing)} uncontracted, dictionary at {revision}"
    )
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
