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
