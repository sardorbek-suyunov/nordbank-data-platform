"""Put `scripts/` on the import path for the whole generator test package.

Several modules under test import from `scripts/` — `schema_contract` for the column type the
drift timeline declares its deltas in, `source_db_driver` and `source_db_exec` for the two
transports. Each of those modules adds the directory itself when it is imported, so the suite
passes when something has already pulled one in and fails when a single test file is run on its
own. That is a collection order dependency, which is the kind of thing that works until someone
runs one test.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
for candidate in (str(ROOT), str(ROOT / "scripts")):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

import pytest  # noqa: E402
from network_guard import install  # noqa: E402


@pytest.fixture(autouse=True)
def _no_external_network(monkeypatch):
    """Spec 006 section 6: no test reaches the internet. See `scripts/network_guard.py`."""
    install(monkeypatch)
