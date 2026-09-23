"""The synthetic sanctions list (ADR 0015, spec 006 criteria 1, 13 and 14 at the source)."""

from __future__ import annotations

import csv
import datetime as dt
import json

from generator.sanctions import build

ANCHOR = dt.date(2026, 7, 20)  # a Monday
PARAMETERS = build.Parameters(
    base_entities=300, weekly_additions=5, weekly_removals=2, pep_share=0.3
)


def _entities(day: dt.date) -> list[dict]:
    publication = build.publish(42, ANCHOR, day, PARAMETERS)
    return [json.loads(line) for line in publication.entities.decode("utf-8").splitlines()]


def _all_names(entity: dict) -> list[str]:
    properties = entity["properties"]
    return [entity["caption"], *properties.get("name", []), *properties.get("alias", [])]


def test_every_name_in_the_list_is_a_marker_name() -> None:
    entities = _entities(ANCHOR + dt.timedelta(days=30))
    names = [name for entity in entities for name in _all_names(entity)]
    assert len(entities) >= 300 and len(names) >= 600
    offenders = [name for name in names if not build.is_marker_name(name)]
    assert offenders == []


def test_the_screening_fixture_is_in_the_list() -> None:
    with build.FIXTURE.open(encoding="utf-8", newline="") as handle:
        fixture = {row["counterparty_name"] for row in csv.DictReader(handle)}
    assert len(fixture) == 12
    listed = {name for entity in _entities(ANCHOR) for name in entity["properties"]["name"]}
    assert fixture <= listed


def test_every_entity_has_the_documented_top_level_keys() -> None:
    documented = {
        "id",
        "schema",
        "caption",
        "datasets",
        "referents",
        "first_seen",
        "last_change",
        "properties",
    }
    entities = _entities(ANCHOR)
    assert entities and all(set(entity) == documented for entity in entities)
    assert all(isinstance(v, list) for e in entities for v in e["properties"].values())


def test_the_version_changes_daily_and_the_content_only_on_mondays() -> None:
    monday = ANCHOR + dt.timedelta(days=7)
    tuesday = monday + dt.timedelta(days=1)
    sunday = monday - dt.timedelta(days=1)
    publications = {d: build.publish(42, ANCHOR, d, PARAMETERS) for d in (sunday, monday, tuesday)}
    versions = {p.version for p in publications.values()}
    assert len(versions) == 3
    assert publications[monday].entities == publications[tuesday].entities
    assert publications[sunday].entities != publications[monday].entities
    before = {e["id"] for e in _entities(sunday)}
    after = {e["id"] for e in _entities(monday)}
    assert len(after - before) == 5 and len(before - after) == 2


def test_a_publication_is_a_function_of_seed_anchor_and_day() -> None:
    day = ANCHOR + dt.timedelta(days=12)
    first = build.publish(42, ANCHOR, day, PARAMETERS)
    again = build.publish(42, ANCHOR, day, PARAMETERS)
    assert (first.version, first.entities, first.index) == (
        again.version,
        again.entities,
        again.index,
    )
    assert build.publish(43, ANCHOR, day, PARAMETERS).entities != first.entities


def test_the_version_is_in_the_publisher_form() -> None:
    version = build.version_for(42, dt.date(2026, 9, 22))
    stamp, suffix = version.split("-")
    assert stamp == "20260922070000" and len(suffix) == 3 and suffix.isalpha()
