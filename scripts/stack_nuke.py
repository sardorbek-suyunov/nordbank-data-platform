"""Stop the stack and delete its volumes.

Interactive by default because it destroys the warehouse and both databases. FORCE=1 skips the
prompt, without which nothing scripted could use this target.
"""

import os
import subprocess
import sys

CONFIRMATION = "nuke"


def main() -> int:
    if os.environ.get("FORCE") != "1":
        print("This removes every volume: the warehouse file, both databases and the lake.")
        try:
            answer = input(f"Type {CONFIRMATION} to confirm: ").strip()
        except EOFError:
            print("no terminal available; re-run with FORCE=1 to skip the prompt", file=sys.stderr)
            return 1
        if answer != CONFIRMATION:
            print("aborted")
            return 1

    return subprocess.run(
        ["docker", "compose", "down", "--volumes", "--remove-orphans"], check=False
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
