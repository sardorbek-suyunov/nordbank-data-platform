"""DAG integrity: every file in the DAG folder parses, and every DAG is described properly.

Marked `dags` because it needs an Airflow installation. It is never skipped.

**Every test here asserts a minimum cardinality** (`docs/conventions.md`, Tests). A test that
loops over the parsed DAGs passes against an empty DagBag, and a test of an ingestion DAG's
shape passes against a DAG with no entities: at M4 a contract root that resolved only inside
the image built two DAGs with no entities and no assets on a runner, and sixteen of eighteen
tests stayed green. So the DAG set, each DAG's task count, each DAG's asset count and each
mapped task's expansion input are all asserted to be at least what they must be.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.dags

DAG_FOLDER = Path(__file__).resolve().parent.parent / "dags"

# The minimum task count of every DAG the folder must contain. A DAG absent from this table
# fails `test_every_dag_declares_its_minimum_task_count`, so adding a DAG means stating its
# shape here rather than inheriting a vacuous pass.
MINIMUM_TASKS: dict[str, int] = {
    "ops_stack_healthcheck": 4,
    "ops_source_tick": 1,
    "ingest_core_banking": 4,
    "ingest_reference_data": 4,
}


def _dagbag():
    # Airflow 3 moved DagBag and dropped include_examples; example DAGs are a separate bundle
    # and are disabled by AIRFLOW__CORE__LOAD_EXAMPLES in compose.
    from airflow.dag_processing.dagbag import DagBag

    return DagBag(dag_folder=str(DAG_FOLDER))


def _dags() -> dict:
    """The parsed DAGs, refusing a set smaller than the one this file states."""
    dags = _dagbag().dags
    assert len(dags) >= len(MINIMUM_TASKS), (
        f"{len(dags)} DAG(s) parsed, at least {len(MINIMUM_TASKS)} expected: {sorted(dags)}"
    )
    return dags


def test_dag_folder_exists() -> None:
    assert DAG_FOLDER.is_dir(), f"DAG folder missing: {DAG_FOLDER}"


def test_no_import_errors() -> None:
    dagbag = _dagbag()
    assert dagbag.import_errors == {}, f"DAG import errors: {dagbag.import_errors}"
    assert len(dagbag.dags) >= len(MINIMUM_TASKS)


def test_every_dag_declares_an_owner_and_tags() -> None:
    for dag_id, dag in _dags().items():
        owners = [owner for owner in (dag.owner or "").split(",") if owner.strip()]
        assert owners and owners != ["airflow"], f"{dag_id} has no meaningful owner"
        assert dag.tags, f"{dag_id} has no tags"


def test_dag_id_matches_its_filename() -> None:
    for dag_id, dag in _dags().items():
        assert Path(dag.fileloc).stem == dag_id, (
            f"{dag_id} is defined in {Path(dag.fileloc).name}; the names must match"
        )


def test_no_cycles() -> None:
    for _dag_id, dag in _dags().items():
        dag.check_cycle()  # raises AirflowDagCycleException


def test_every_dag_declares_its_minimum_task_count() -> None:
    dags = _dags()
    undeclared = sorted(set(dags) - set(MINIMUM_TASKS))
    assert not undeclared, f"DAG(s) with no minimum task count in this file: {undeclared}"
    missing = sorted(set(MINIMUM_TASKS) - set(dags))
    assert not missing, f"DAG(s) this file expects that did not parse: {missing}"
    for dag_id, minimum in MINIMUM_TASKS.items():
        count = len(dags[dag_id].tasks)
        assert count >= minimum, f"{dag_id} has {count} task(s), at least {minimum} expected"


def test_python_files_and_parsed_dags_agree() -> None:
    modules = {path.stem for path in DAG_FOLDER.glob("*.py") if not path.name.startswith("_")}
    parsed = set(_dags())
    assert len(modules) >= len(MINIMUM_TASKS)
    assert modules == parsed, f"DAG modules {modules} do not match parsed DAG ids {parsed}"


def test_the_source_tick_is_paused_and_touches_no_warehouse_pool() -> None:
    """Spec 004 section 6 and acceptance criterion 12.

    Paused because M3 proves the wiring and M4 decides when to run it. No warehouse pool because
    it touches only the source database, and taking a slot it does not need would block a DAG
    that does.
    """
    dag = _dagbag().dags.get("ops_source_tick")
    assert dag is not None, "ops_source_tick did not parse"
    assert dag.is_paused_upon_creation is True
    assert len(dag.tasks) >= MINIMUM_TASKS["ops_source_tick"]
    for task in dag.tasks:
        assert task.pool == "default_pool", f"{task.task_id} holds pool {task.pool}"


def test_the_simulation_writes_through_a_connection_of_its_own() -> None:
    """The only thing in this repository that writes to `core` does not share the read-only id.

    `docs/architecture.md` draws the line and names the connection, so the name is asserted
    rather than left to a reader to notice.
    """
    source = (DAG_FOLDER / "ops_source_tick.py").read_text(encoding="utf-8")
    assert "nordbank_source_simulator" in source
    assert "nordbank_source_db" not in source.split('"""', 2)[2]


INGEST_DAGS = ("ingest_core_banking", "ingest_reference_data")

# The entity counts spec 005 fixes: sixteen in `core`, twenty-nine in `ref`.
EXPECTED_ENTITIES = {"ingest_core_banking": 16, "ingest_reference_data": 29}


@pytest.mark.parametrize("dag_id", INGEST_DAGS)
def test_only_the_pooled_phases_hold_the_warehouse_pool(dag_id: str) -> None:
    """Spec 005 section 3, and the trap the DAG factory carries a comment about.

    Setting `pool` in `default_args` would hand `warehouse_access` to every mapped extract task
    and serialise the whole extraction through one slot. The measured behaviour is that a task
    with no declared pool takes `default_pool`, so the assertion is on both directions rather
    than only on the pooled pair.
    """
    dag = _dagbag().dags[dag_id]
    assert len(dag.tasks) >= MINIMUM_TASKS[dag_id]
    pooled = {task.task_id for task in dag.tasks if task.pool == "warehouse_access"}
    assert pooled == {"open_batches", "register"}, f"{dag_id} pools: {pooled}"
    for task in dag.tasks:
        if task.task_id not in pooled:
            assert task.pool == "default_pool", f"{task.task_id} holds pool {task.pool}"


@pytest.mark.parametrize("dag_id", INGEST_DAGS)
def test_register_and_gate_run_on_all_done(dag_id: str) -> None:
    """One entity's breaking drift must not stop the other forty-four from registering."""
    dag = _dagbag().dags[dag_id]
    assert str(dag.get_task("register").trigger_rule) == "TriggerRule.ALL_DONE"
    assert str(dag.get_task("gate").trigger_rule) == "TriggerRule.ALL_DONE"


@pytest.mark.parametrize("dag_id", INGEST_DAGS)
def test_the_ingestion_dags_are_unscheduled_with_catchup_off(dag_id: str) -> None:
    """The source's clock is simulated, so a wall-clock schedule has no meaning against it.

    Measured before it was decided: unpausing a `catchup=True` DAG with a past start date
    creates and runs one scheduled run per elapsed interval at once, which would race the
    backfill loop and register batches for days the tick had not produced.
    """
    dag = _dagbag().dags[dag_id]
    assert dag.schedule is None, f"{dag_id} is scheduled: {dag.schedule}"
    assert dag.catchup is False


@pytest.mark.parametrize("dag_id", INGEST_DAGS)
def test_one_asset_is_emitted_per_entity(dag_id: str) -> None:
    dag = _dagbag().dags[dag_id]
    outlets = dag.get_task("register").outlets
    assert len(outlets) == EXPECTED_ENTITIES[dag_id]
    names = {getattr(outlet, "name", None) for outlet in outlets}
    assert all(name and name.startswith(f"{dag_id}/") for name in names), names


@pytest.mark.parametrize("dag_id", INGEST_DAGS)
def test_the_extract_task_is_mapped_over_the_open_step(dag_id: str) -> None:
    """The wiring half of the mapped-input rule.

    The expansion input is the open step's return value, which exists only at run time, so a
    DAG-level test can prove where it comes from and not that it is non-empty. The other half
    is `test_open_phase.py`, which runs the open step against a throwaway warehouse and
    asserts one allocation per contracted entity.
    """
    dag = _dagbag().dags[dag_id]
    extract = dag.get_task("extract")
    assert extract.upstream_task_ids == {"open_batches"}
    assert "register" in extract.downstream_task_ids
    # On a decorated mapped task `expand_input` is an empty placeholder and the real input is
    # `op_kwargs_expand_input`. Measured in Airflow 3.3.2: asserting on the first reports a
    # correctly wired DAG as mapped over nothing, and a test that only checked its type would
    # have passed on the placeholder.
    expand = getattr(extract, "op_kwargs_expand_input", None) or extract.expand_input
    assert len(expand.value) >= 1, f"{dag_id}: extract is mapped over nothing"
    sources = {getattr(operator, "task_id", None) for operator, _key in expand.iter_references()}
    assert sources == {"open_batches"}, f"{dag_id}: extract is mapped over {sources}"


def test_the_contract_root_resolves_in_this_layout() -> None:
    """Whichever layout the tests run in, the DAG factory must find the contracts.

    Asserted directly because the symptom of not finding them is not an error: it is two DAGs
    with no entities, no assets and nothing to ingest, and only the asset count notices.
    """
    from nordbank_ops.ingest import contract_root, entities

    assert contract_root().is_dir()
    assert len(entities("core")) == EXPECTED_ENTITIES["ingest_core_banking"]
    assert len(entities("ref")) == EXPECTED_ENTITIES["ingest_reference_data"]
