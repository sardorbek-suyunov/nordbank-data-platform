"""Classify the source's shape against the contract in force (spec 005 section 8).

Two outcomes and no third. An **additive** column — present in the source, absent from the
contract — lands the batch, is not written to bronze, and is recorded. A **breaking** change —
a type change, a removed column, or a changed primary key — quarantines the whole batch, marks
it `failed`, leaves the watermark unmoved and fails the task.

"No partial load" is a guarantee at entity grain, not at run grain: the entities that extracted
cleanly in the same run are still registered, and a terminal gate fails the run. One entity's
drift stopping all forty-five would be a worse failure than the drift.

Only one of the three breaking kinds is reachable from the generator's scripted timeline: the
widening of `core.payments.remittance_reference` at anchor plus thirty-seven days. A removed
column and a changed primary key are proven by unit test against a fabricated source schema and
by nothing else, which spec 005 criterion 13 says rather than implies. Scripting them is a
change to the generator's drift timeline and belongs to a later milestone.

The comparison is against the **live catalogue**, not against the data dictionary. The
dictionary is a committed file that the drift events deliberately do not edit, so comparing
against it would detect nothing at ingest, which is the only place detection matters.
"""

from __future__ import annotations

from dataclasses import dataclass

ADDITIVE = "additive"
TYPE_CHANGED = "type_changed"
COLUMN_REMOVED = "column_removed"
PRIMARY_KEY_CHANGED = "primary_key_changed"

BREAKING_KINDS: frozenset[str] = frozenset({TYPE_CHANGED, COLUMN_REMOVED, PRIMARY_KEY_CHANGED})

ACTION_LANDED = "landed, column omitted from bronze"
ACTION_FAILED = "batch quarantined, watermark unmoved"


@dataclass(frozen=True)
class DriftObservation:
    column: str
    kind: str
    detail: str

    @property
    def is_breaking(self) -> bool:
        return self.kind in BREAKING_KINDS

    @property
    def action(self) -> str:
        return ACTION_FAILED if self.is_breaking else ACTION_LANDED


@dataclass(frozen=True)
class DriftReport:
    observations: tuple[DriftObservation, ...]

    @property
    def breaking(self) -> tuple[DriftObservation, ...]:
        return tuple(o for o in self.observations if o.is_breaking)

    @property
    def additive(self) -> tuple[DriftObservation, ...]:
        return tuple(o for o in self.observations if not o.is_breaking)

    @property
    def is_breaking(self) -> bool:
        return bool(self.breaking)

    def summary(self) -> str:
        if not self.observations:
            return "no drift"
        return "; ".join(f"{o.kind} on {o.column}: {o.detail}" for o in self.observations)


def classify(contract, live_types: dict[str, str], live_primary_key: str | None) -> DriftReport:
    """Compare a contract against the source's current shape.

    `live_types` maps column name to the type as `information_schema` spells it, which is the
    same spelling the dictionary and therefore the contract use.
    """
    observations: list[DriftObservation] = []

    contracted = {column.name: column for column in contract.columns}

    for name in sorted(set(live_types) - set(contracted)):
        observations.append(
            DriftObservation(
                column=name,
                kind=ADDITIVE,
                detail=f"source has {live_types[name]}, contract has no such column",
            )
        )

    for name in sorted(set(contracted) - set(live_types)):
        observations.append(
            DriftObservation(
                column=name,
                kind=COLUMN_REMOVED,
                detail=f"contract has {contracted[name].data_type}, source has no such column",
            )
        )

    for name in sorted(set(contracted) & set(live_types)):
        declared = contracted[name].data_type
        actual = live_types[name]
        if declared != actual:
            observations.append(
                DriftObservation(
                    column=name,
                    kind=TYPE_CHANGED,
                    detail=f"contract has {declared}, source has {actual}",
                )
            )

    if live_primary_key is not None and live_primary_key != contract.primary_key:
        observations.append(
            DriftObservation(
                column=live_primary_key,
                kind=PRIMARY_KEY_CHANGED,
                detail=f"contract keys on {contract.primary_key}, source keys on "
                f"{live_primary_key}",
            )
        )

    return DriftReport(observations=tuple(observations))
