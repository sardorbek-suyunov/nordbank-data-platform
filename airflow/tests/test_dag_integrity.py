"""DAG integrity: every file in the DAG folder parses, and every DAG is described properly.

Marked `dags` because it needs an Airflow installation. It passes when the folder is empty and
is never skipped: an empty DAG folder is a valid state, an unimportable one is not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.dags

DAG_FOLDER = Path(__file__).resolve().parent.parent / "dags"


def _dagbag():
    # Airflow 3 moved DagBag and dropped include_examples; example DAGs are a separate bundle
    # and are disabled by AIRFLOW__CORE__LOAD_EXAMPLES in compose.
    from airflow.dag_processing.dagbag import DagBag

    return DagBag(dag_folder=str(DAG_FOLDER))


def test_dag_folder_exists() -> None:
    assert DAG_FOLDER.is_dir(), f"DAG folder missing: {DAG_FOLDER}"


def test_no_import_errors() -> None:
    dagbag = _dagbag()
    assert dagbag.import_errors == {}, f"DAG import errors: {dagbag.import_errors}"


def test_every_dag_declares_an_owner_and_tags() -> None:
    for dag_id, dag in _dagbag().dags.items():
        owners = [owner for owner in (dag.owner or "").split(",") if owner.strip()]
        assert owners and owners != ["airflow"], f"{dag_id} has no meaningful owner"
        assert dag.tags, f"{dag_id} has no tags"


def test_dag_id_matches_its_filename() -> None:
    for dag_id, dag in _dagbag().dags.items():
        assert Path(dag.fileloc).stem == dag_id, (
            f"{dag_id} is defined in {Path(dag.fileloc).name}; the names must match"
        )


def test_no_cycles() -> None:
    for dag_id, dag in _dagbag().dags.items():
        dag.check_cycle()  # raises AirflowDagCycleException
        assert len(dag.tasks) > 0, f"{dag_id} has no tasks"


def test_python_files_and_parsed_dags_agree() -> None:
    modules = {path.stem for path in DAG_FOLDER.glob("*.py") if not path.name.startswith("_")}
    parsed = set(_dagbag().dags)
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
    dag = _dagbag().dags[dag_id]
    extract = dag.get_task("extract")
    assert extract.upstream_task_ids == {"open_batches"}
    assert "register" in extract.downstream_task_ids


def test_the_contract_root_resolves_in_this_layout() -> None:
    """Whichever layout the tests run in, the DAG factory must find the contracts.

    Asserted directly because the symptom of not finding them is not an error: it is two DAGs
    with no entities, no assets and nothing to ingest, and only the asset count notices.
    """
    from nordbank_ops.ingest import contract_root, entities

    assert contract_root().is_dir()
    assert len(entities("core")) == EXPECTED_ENTITIES["ingest_core_banking"]
    assert len(entities("ref")) == EXPECTED_ENTITIES["ingest_reference_data"]
