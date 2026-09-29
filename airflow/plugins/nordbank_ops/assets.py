"""Asset events for the batches a run registered, and for nothing else.

One event per registered batch, on the asset `<dag_id>/<entity>`, carrying the batch id, the
rows landed and the batch's `empty_reason`, so a registered empty batch emits and says why it is
empty. A batch that failed emits nothing, and a run whose batches all failed emits nothing.

**Why through an alias.** Airflow emits an event for every `Asset` declared as a task outlet
whenever the task succeeds, whatever the task did; measured on Airflow 3.3.2, the task runner
sends the declared outlets on success and the API server registers one event for each, with no
way for the task to withhold one. The register step runs on `all_done` and succeeds when it has
registered nothing, so declared assets announced batches that did not exist: on 2026-08-20 in
the third acceptance run a settlement run with both batches failed emitted two events, and a
cleared re-run that registered nothing emitted again. An `AssetAlias` outlet emits exactly the
events added to it, so the register step declares one alias per DAG and adds an event per batch
it registered.
"""

from __future__ import annotations

from typing import Any

ALIAS = "registered"


def alias_name(dag_id: str) -> str:
    return f"{dag_id}/{ALIAS}"


def asset_name(dag_id: str, entity: str) -> str:
    return f"{dag_id}/{entity}"


def described(dag_id: str, entities: tuple[str, ...]) -> str:
    """The register step's task documentation, naming every asset it can emit.

    The alias hides the assets from the task's declared outlets, so the names are stated here,
    where the Airflow UI shows them and the DAG tests read them.
    """
    names = "\n".join(f"- `{asset_name(dag_id, entity)}`" for entity in entities)
    return (
        f"Registers the run's batches, then emits one event per registered batch through the "
        f"alias `{alias_name(dag_id)}`, on one of these assets:\n\n{names}\n"
    )


def emittable(doc: str) -> set[str]:
    """The asset names a register step's documentation says it can emit."""
    return {line[3:-1] for line in doc.splitlines() if line.startswith("- `")}


def events(dag_id: str, entities: tuple[str, ...], batches: list[dict]) -> list[tuple[str, dict]]:
    """The (asset name, extra) pairs for a run's registered batches, one per batch.

    `entities` is the DAG's own list. A batch for an entity outside it is a wiring fault rather
    than an event, and raises.
    """
    out = []
    for batch in batches:
        if batch["entity"] not in entities:
            raise ValueError(f"{dag_id} has no asset for entity {batch['entity']!r}")
        out.append(
            (
                asset_name(dag_id, batch["entity"]),
                {
                    "batch_id": batch["batch_id"],
                    "rows_landed": batch["rows_landed"],
                    "empty_reason": batch.get("empty_reason"),
                },
            )
        )
    return out


def emit(outlet_events: Any, dag_id: str, entities: tuple[str, ...], batches: list[dict]) -> int:
    """Add the run's events to the register step's alias. Returns how many were added."""
    from airflow.sdk import Asset, AssetAlias

    accessor = outlet_events[AssetAlias(name=alias_name(dag_id))]
    pairs = events(dag_id, entities, batches)
    for name, extra in pairs:
        accessor.add(Asset(name=name), extra=extra)
    print(f"register: {len(pairs)} asset event(s) for {len(batches)} registered batch(es)")
    return len(pairs)
