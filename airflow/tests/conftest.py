import sys
from pathlib import Path

# These tests run from two layouts: the repository (<repo>/airflow/tests, with scripts at
# <repo>/scripts) and the project image (/opt/airflow/tests, with scripts at
# /opt/airflow/scripts). Add whichever of the candidates exists.
BASE = Path(__file__).resolve().parent.parent

for directory in (BASE / "plugins", BASE / "scripts", BASE.parent / "scripts"):
    if directory.is_dir() and str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import pytest  # noqa: E402
from network_guard import install  # noqa: E402


@pytest.fixture(autouse=True)
def _no_external_network(monkeypatch):
    """Spec 006 section 6: no test reaches the internet. See `scripts/network_guard.py`."""
    install(monkeypatch)
