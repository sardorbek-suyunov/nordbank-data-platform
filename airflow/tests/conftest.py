import sys
from pathlib import Path

# These tests run from two layouts: the repository (<repo>/airflow/tests, with scripts at
# <repo>/scripts) and the project image (/opt/airflow/tests, with scripts at
# /opt/airflow/scripts). Add whichever of the candidates exists.
BASE = Path(__file__).resolve().parent.parent

for directory in (BASE / "plugins", BASE / "scripts", BASE.parent / "scripts"):
    if directory.is_dir() and str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
