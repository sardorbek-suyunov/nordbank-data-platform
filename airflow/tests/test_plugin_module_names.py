"""No module under the plugins folder may share a name with a standard-library module.

Airflow's plugin loader executes every `.py` file under the plugins folder, at any depth, and
registers it in `sys.modules` under its bare file stem (`_shared/plugins_manager`, measured in
Airflow 3.3.2). A file named `http.py` inside `nordbank_ops/feeds/` therefore replaced the
standard library's `http` in every Airflow process: the scheduler, the API server and the DAG
processor all died at start-up on `from http import HTTPStatus`, three imports deep inside
uvicorn, with nothing in the traceback pointing at the plugin loader.

The package is imported properly through PYTHONPATH as well, so the only effect of the loader's
registration is the shadowing, and this test is what stops it recurring under another name.
"""

from __future__ import annotations

import sys
from pathlib import Path

PLUGINS = Path(__file__).resolve().parent.parent / "plugins"


def test_no_plugin_module_shadows_the_standard_library() -> None:
    modules = [path for path in PLUGINS.rglob("*.py") if path.stem != "__init__"]
    assert len(modules) >= 15, f"only {len(modules)} plugin module(s) found under {PLUGINS}"
    clashes = sorted(
        str(path.relative_to(PLUGINS)) for path in modules if path.stem in sys.stdlib_module_names
    )
    assert clashes == [], f"plugin modules named like standard-library modules: {clashes}"
