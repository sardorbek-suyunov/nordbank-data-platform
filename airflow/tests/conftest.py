import sys
from pathlib import Path

PLUGINS = Path(__file__).resolve().parent.parent / "plugins"
if str(PLUGINS) not in sys.path:
    sys.path.insert(0, str(PLUGINS))
