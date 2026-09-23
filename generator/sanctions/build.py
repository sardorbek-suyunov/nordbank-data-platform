"""Build one day's publication of the synthetic list. Pure: no object store."""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import random
from dataclasses import dataclass
from pathlib import Path

from generator.rng import SubStreams

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "screening_positives.csv"
MARKER_PREFIX = "ZZ-"
MARKER_SUFFIX = "SANCTIONS-FIXTURE"
DATASET = "zz_synthetic_sanctions"

WORDS = (
    "ALPHA", "BRAVO", "CHARLIE", "DELTA", "ECHO", "FOXTROT", "GOLF", "HOTEL", "INDIA",
    "JULIETT", "KILO", "LIMA", "MIKE", "NOVEMBER", "OSCAR", "PAPA", "QUEBEC", "ROMEO",
    "SIERRA", "TANGO", "UNIFORM", "VICTOR", "WHISKEY", "XRAY", "YANKEE", "ZULU",
)  # fmt: skip
COUNTRIES = ("zz", "xa", "xb", "xc", "xd")  # user-assigned ISO 3166 codes: no real country


@dataclass(frozen=True)
class Parameters:
    base_entities: int
    weekly_additions: int
    weekly_removals: int
    pep_share: float

    @classmethod
    def from_profile(cls, section: dict) -> Parameters:
        return cls(
            base_entities=int(section["base_entities"]),
            weekly_additions=int(section["weekly_additions"]),
            weekly_removals=int(section["weekly_removals"]),
            pep_share=float(section["pep_share"]),
        )


@dataclass
class Publication:
    version: str
    last_export: str
    entities: bytes
    index: bytes
    entity_count: int


def marker_name(*parts: str) -> str:
    return " ".join((f"{MARKER_PREFIX}SYNTHETIC", *parts, MARKER_SUFFIX))


def is_marker_name(name: str) -> bool:
    return name.startswith(MARKER_PREFIX) and name.endswith(MARKER_SUFFIX)


def _stamp(day: dt.date) -> str:
    return f"{day.isoformat()}T07:00:00"


def _entity(serial: int, rng: random.Random, first_seen: dt.date, pep_share: float) -> dict:
    is_person = rng.random() < 0.7
    word = WORDS[serial % len(WORDS)]
    name = marker_name(word, f"{serial:04d}")
    properties: dict[str, list[str]] = {"name": [name]}
    if rng.random() < 0.4:
        properties["alias"] = [marker_name(word, f"{serial:04d}", "ALIAS")]
    topics = ["role.pep"] if rng.random() < pep_share else ["sanction"]
    properties["topics"] = topics
    country = rng.choice(COUNTRIES)
    if is_person:
        properties["birthDate"] = [f"{rng.randint(1940, 2000)}"]
        properties["nationality"] = [country]
    else:
        properties["country"] = [country]
    return {
        "id": "NK-ZZ" + hashlib.sha256(f"entity-{serial}".encode()).hexdigest()[:20],
        "schema": "Person" if is_person else "Organization",
        "caption": name,
        "datasets": [DATASET],
        "referents": [f"zz-synth-{serial:05d}"],
        "first_seen": _stamp(first_seen),
        "last_change": _stamp(first_seen),
        "properties": properties,
    }


def fixture_entities(first_seen: dt.date) -> list[dict]:
    """The M3 screening fixture, as list entities, so a payment drawn from it can match."""
    with FIXTURE.open(encoding="utf-8", newline="") as handle:
        names = [row["counterparty_name"] for row in csv.DictReader(handle)]
    out = []
    for name in names:
        is_person = name.startswith("ZZ-TESTCASE")
        out.append(
            {
                "id": "NK-ZZ" + hashlib.sha256(f"fixture-{name}".encode()).hexdigest()[:20],
                "schema": "Person" if is_person else "Organization",
                "caption": name,
                "datasets": [DATASET],
                "referents": ["zz-fixture-" + hashlib.sha256(name.encode()).hexdigest()[:8]],
                "first_seen": _stamp(first_seen),
                "last_change": _stamp(first_seen),
                "properties": {"name": [name], "topics": ["sanction"]},
            }
        )
    return out


def mondays(anchor: dt.date, day: dt.date) -> list[dt.date]:
    first = anchor + dt.timedelta(days=(7 - anchor.weekday()) % 7)
    out = []
    while first <= day:
        out.append(first)
        first += dt.timedelta(days=7)
    return out


def entities_on(seed: int, anchor: dt.date, day: dt.date, parameters: Parameters) -> list[dict]:
    streams = SubStreams(seed)
    listed_since = anchor - dt.timedelta(days=365)
    base_rng = streams.stream("sanctions.base")
    live: dict[int, dict] = {
        serial: _entity(serial, base_rng, listed_since, parameters.pep_share)
        for serial in range(parameters.base_entities)
    }
    serial = parameters.base_entities
    for monday in mondays(anchor, day):
        rng = streams.stream("sanctions.week", monday.isoformat())
        for serial_removed in rng.sample(
            sorted(live), k=min(parameters.weekly_removals, len(live))
        ):
            del live[serial_removed]
        for _ in range(parameters.weekly_additions):
            live[serial] = _entity(serial, rng, monday, parameters.pep_share)
            serial += 1
    return sorted([*live.values(), *fixture_entities(listed_since)], key=lambda e: e["id"])


def version_for(seed: int, day: dt.date) -> str:
    suffix_rng = SubStreams(seed).stream("sanctions.version", day.isoformat())
    suffix = "".join(suffix_rng.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(3))
    return f"{day:%Y%m%d}070000-{suffix}"


def publish(seed: int, anchor: dt.date, day: dt.date, parameters: Parameters) -> Publication:
    entities = entities_on(seed, anchor, day, parameters)
    body = "".join(json.dumps(entity, ensure_ascii=False) + "\n" for entity in entities)
    body_bytes = body.encode("utf-8")
    version = version_for(seed, day)
    index = {
        "name": "sanctions",
        "title": "Consolidated Sanctions (synthetic)",
        "version": version,
        "last_export": _stamp(day),
        "updated_at": _stamp(day),
        "entity_count": len(entities),
        "coverage": {"frequency": "daily", "schedule": "0 7 * * *"},
        "resources": [
            {
                "name": "entities.ftm.json",
                "mime_type": "application/json+ftm",
                "size": len(body_bytes),
            }
        ],
    }
    return Publication(
        version=version,
        last_export=_stamp(day),
        entities=body_bytes,
        index=json.dumps(index, indent=2).encode("utf-8"),
        entity_count=len(entities),
    )
