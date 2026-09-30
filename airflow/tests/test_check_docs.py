"""The docs check enforces the reissue protocol (specification 006's final review, F5).

A specification at version 2 or higher must carry a `Supersedes:` line and a `## Changelog`
section. Each is removed in turn from an otherwise complete reissue to show the check fails,
and the specifications reissued so far are checked as they stand.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from check_docs import reissue_failures, reissue_failures_for

SPECS = Path(__file__).resolve().parent.parent.parent / "docs" / "specs"

COMPLETE = """# 999 — Example

Status: Approved
Version: 2
Supersedes: version 1
Depends on: 000

## Goal
Something.

## Changelog

- What changed.

## Amendments
"""


def test_a_complete_reissue_passes():
    assert reissue_failures_for("999.md", COMPLETE) == []


def test_a_reissue_without_a_supersedes_line_fails():
    text = COMPLETE.replace("Supersedes: version 1\n", "")
    assert reissue_failures_for("999.md", text) == ["999.md: version 2 has no 'Supersedes:' line"]


def test_a_reissue_without_a_changelog_fails():
    text = COMPLETE.replace("## Changelog\n", "## Notes\n")
    assert reissue_failures_for("999.md", text) == [
        "999.md: version 2 has no '## Changelog' section"
    ]


def test_a_first_version_needs_neither():
    text = COMPLETE.replace("Version: 2\n", "").replace("Supersedes: version 1\n", "")
    assert reissue_failures_for("999.md", text.replace("## Changelog\n", "")) == []


@pytest.mark.parametrize(
    "name",
    [
        "001-local-stack.md",
        "002-source-system-schema.md",
        "005-extraction-and-bronze-landing.md",
        "006-external-feeds.md",
        "007-dbt-bronze-models.md",
    ],
)
def test_the_reissued_specifications_pass_as_they_stand(name):
    text = (SPECS / name).read_text(encoding="utf-8")
    assert "Version: 2" in text.splitlines()
    assert reissue_failures_for(name, text) == []


def test_the_whole_tree_passes():
    assert reissue_failures() == []
