"""Documentation checks for CI.

Three rules:

1. Every specification and ADR carries a `Status:` line. Directory READMEs inside `docs/adr` and
   `docs/specs` describe the directory rather than a decision and are exempt.
2. The root README carries no TODO token.
3. The contract bands in `docs/generator_realism.md` agree with `generator/profiles.yml`.

The third exists because those bands are one fact with two representations, and neither can be
deleted. Spec 003's invariants and acceptance criteria are written against the bands *as the
document states them*, so a realism document that stated none could not be reviewed for whether
its bands are sensible. The generator has to read them to act on them. Where a duplicate is
unavoidable, a check asserts the two agree rather than leaving it to discipline — the rule is in
`docs/specs/README.md`.
"""

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
STATUS_DIRS = (ROOT / "docs" / "adr", ROOT / "docs" / "specs")
README = ROOT / "README.md"
PROFILES = ROOT / "generator" / "profiles.yml"
REALISM = ROOT / "docs" / "generator_realism.md"

# | `band_name` | 0.0005 | 0.0015 |   or   | `band_name` | 0.006 | — |
BAND_ROW = re.compile(
    r"^\|\s*`(?P<name>[a-z0-9_]+)`\s*\|\s*(?P<low>[0-9.]+)\s*\|\s*(?P<high>[0-9.]+|—|-)\s*\|"
)


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


def _documented_bands() -> dict[str, tuple[float, float | None]]:
    if not REALISM.exists():
        return {}
    bands: dict[str, tuple[float, float | None]] = {}
    for line in REALISM.read_text(encoding="utf-8").splitlines():
        match = BAND_ROW.match(line.strip())
        if not match:
            continue
        high = match.group("high")
        bands[match.group("name")] = (
            float(match.group("low")),
            None if high in ("—", "-") else float(high),
        )
    return bands


def _configured_bands() -> dict[str, tuple[float, float | None]]:
    raw = yaml.safe_load(PROFILES.read_text(encoding="utf-8"))
    configured: dict[str, tuple[float, float | None]] = {}
    for name, value in raw["defaults"]["bands"].items():
        if isinstance(value, dict):
            configured[name] = (float(value["min"]), float(value["max"]))
        else:
            configured[name] = (float(value), None)
    return configured


def band_failures() -> list[str]:
    if not REALISM.exists() or not PROFILES.exists():
        return []
    documented = _documented_bands()
    configured = _configured_bands()

    failures = []
    for name in sorted(set(documented) | set(configured)):
        in_doc = documented.get(name)
        in_config = configured.get(name)
        if in_doc is None:
            failures.append(
                f"generator_realism.md: band {name!r} is in profiles.yml but not in the "
                f"contract bands table"
            )
        elif in_config is None:
            failures.append(
                f"profiles.yml: band {name!r} is in generator_realism.md but not configured"
            )
        elif in_doc != in_config:
            failures.append(
                f"band {name!r}: generator_realism.md says {in_doc}, profiles.yml says "
                f"{in_config}"
            )
    return failures


def main() -> int:
    failures = status_failures() + readme_failures() + band_failures()
    for failure in failures:
        print(failure)

    if failures:
        print(f"documentation checks: {len(failures)} failure(s)")
        return 1

    documented = len(_documented_bands())
    print(f"documentation checks passed ({documented} contract band(s) agree with profiles.yml)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
