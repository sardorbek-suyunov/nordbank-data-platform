"""Documentation checks for CI.

Two rules: every specification and ADR carries a `Status:` line, and the root README carries
no TODO token. Directory READMEs inside docs/adr and docs/specs describe the directory rather
than a decision, so they are exempt from the status rule.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATUS_DIRS = (ROOT / "docs" / "adr", ROOT / "docs" / "specs")
README = ROOT / "README.md"


def status_failures() -> list[str]:
    failures = []
    for directory in STATUS_DIRS:
        for path in sorted(directory.glob("*.md")):
            if path.name == "README.md":
                continue
            text = path.read_text(encoding="utf-8")
            if not any(line.startswith("Status:") for line in text.splitlines()):
                failures.append(f"{path.relative_to(ROOT).as_posix()}: no 'Status:' line")
    return failures


def readme_failures() -> list[str]:
    if "TODO" in README.read_text(encoding="utf-8"):
        return ["README.md: contains a TODO token"]
    return []


def main() -> int:
    failures = status_failures() + readme_failures()
    for failure in failures:
        print(failure)

    if failures:
        print(f"documentation checks: {len(failures)} failure(s)")
        return 1

    print("documentation checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
