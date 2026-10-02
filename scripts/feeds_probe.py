"""Compare the live feed APIs with the recorded fixtures, or re-record them.

`make feeds-probe` calls every endpoint a fixture was recorded from and compares the **shape**
of what comes back — status, top-level keys and their kinds, the landing rule's outcome —
with the fixture, and reports each fixture's age. It is how an upstream change is noticed:
the test suite runs against the recordings and cannot see one (spec 006 section 6).

`make feeds-probe RECORD=1` re-records every fixture and its sidecar. Re-record when:

1. this probe reports a shape difference;
2. a feed's contract version is bumped, because the fixtures are validated against the
   contract's fingerprint and a required unit test fails when they disagree;
3. a fixture passes the age this probe reports against, 180 days;
4. a live run fails in a way the recorded suite does not.

The probe needs the network and is never part of `make test`. It exits 1 on a shape difference
and 0 otherwise; age is reported, not failed, because a check that goes red on the calendar
teaches people to ignore it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from data_contract import load_history  # noqa: E402

FIXTURES = ROOT / "airflow" / "tests" / "fixtures" / "feeds"
MAX_AGE_DAYS = 180
KEPT_HEADERS = ("content-type", "deprecation", "link", "cache-control")

# What is recorded, and why each one is worth a file.
FRANKFURTER = "https://api.frankfurter.dev"
RECORDINGS: tuple[dict, ...] = (
    {
        "name": "fx_2026-07-24",
        "url": f"{FRANKFURTER}/v1/2026-07-24",
        "params": {"base": "EUR"},
        "why": "a Friday: a publication date, lands every currency",
        "contract": ("ecb", "fx_rates"),
    },
    {
        "name": "fx_2026-07-25",
        "url": f"{FRANKFURTER}/v1/2026-07-25",
        "params": {"base": "EUR"},
        "why": "a Saturday: answered with Friday's date, lands nothing",
        "contract": ("ecb", "fx_rates"),
    },
    {
        "name": "fx_2026-05-01",
        "url": f"{FRANKFURTER}/v1/2026-05-01",
        "params": {"base": "EUR"},
        "why": "a TARGET holiday, Labour Day: answered with the day before, lands nothing",
        "contract": ("ecb", "fx_rates"),
    },
    {
        "name": "fx_2026-04-03",
        "url": f"{FRANKFURTER}/v1/2026-04-03",
        "params": {"base": "EUR"},
        "why": "a TARGET holiday, Good Friday: answered with the day before, lands nothing",
        "contract": ("ecb", "fx_rates"),
    },
    {
        "name": "fx_2026-12-25",
        "url": f"{FRANKFURTER}/v1/2026-12-25",
        "params": {"base": "EUR"},
        "why": "beyond all published data: HTTP 404, absent",
        "contract": ("ecb", "fx_rates"),
    },
    # The CI window, 2026-07-20 to 2026-07-26 (spec 007): the stack job ingests it through the
    # real DAGs with no live call, served from these recordings. 07-24 and 07-25 are above.
    *(
        {
            "name": f"fx_2026-07-{day}",
            "url": f"{FRANKFURTER}/v1/2026-07-{day}",
            "params": {"base": "EUR"},
            "why": why,
            "contract": ("ecb", "fx_rates"),
        }
        for day, why in (
            ("20", "the CI window: a Monday, a publication date"),
            ("21", "the CI window: a publication date"),
            ("22", "the CI window: a publication date"),
            ("23", "the CI window: a publication date"),
            ("26", "the CI window: a Sunday, answered with Friday's date, lands nothing"),
        )
    ),
    # The ci book's FX history (spec 008's prerequisite fix): the one time-series request the
    # backfill makes before its first day, from the day before the book's earliest business
    # instant, 2024-01-24, to the day before the acceptance anchor. The stack job replays it.
    {
        "name": "fx_history_ci",
        "url": f"{FRANKFURTER}/v1/2024-01-23..2026-07-19",
        "params": {"base": "EUR"},
        "why": "the ci FX history: one request, every publication 2024-01-23 to 2026-07-17",
        "contract": ("ecb", "fx_rates"),
    },
    {
        "name": "fx_v2_rates",
        "url": f"{FRANKFURTER}/v2/rates",
        "params": {"base": "EUR", "date": "2026-07-25"},
        "why": "the advertised successor: a list, per-currency dates, not a drop-in",
        "contract": ("ecb", "fx_rates"),
    },
    {
        "name": "fred_without_key",
        "url": "https://api.stlouisfed.org/fred/series/observations",
        "params": {"series_id": "UNRATE", "file_type": "json"},
        "why": "no key: HTTP 400, which is why the feed skips",
        "contract": ("fred", "series"),
    },
)


def _shape(value) -> object:
    if isinstance(value, dict):
        return (
            {key: _shape(inner) for key, inner in sorted(value.items())}
            if len(value) < 20
            else {"<object>": len(value) > 0}
        )
    if isinstance(value, list):
        return ["<list>", _shape(value[0]) if value else None]
    return type(value).__name__


def _get(recording: dict):
    import requests

    return requests.get(recording["url"], params=recording["params"], timeout=30)


def record(only: set[str] | None = None) -> int:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    for recording in RECORDINGS:
        if only and recording["name"] not in only:
            continue
        response = _get(recording)
        system, entity = recording["contract"]
        contract = load_history(ROOT / "contracts" / system)[entity][-1]
        (FIXTURES / f"{recording['name']}.body").write_bytes(response.content)
        sidecar = {
            "captured_at": now.isoformat(),
            "endpoint": recording["url"],
            "params": recording["params"],
            "status": response.status_code,
            "headers": {k: v for k, v in response.headers.items() if k.lower() in KEPT_HEADERS},
            "why": recording["why"],
            "contract": f"{system}.{entity}",
            "contract_version": contract.contract_version,
            "contract_fingerprint": contract.fingerprint,
        }
        (FIXTURES / f"{recording['name']}.meta.json").write_text(
            json.dumps(sidecar, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        print(f"feeds-probe: recorded {recording['name']}: HTTP {response.status_code}")
    return 0


def probe() -> int:
    differences = 0
    today = dt.datetime.now(dt.UTC)
    for recording in RECORDINGS:
        meta_path = FIXTURES / f"{recording['name']}.meta.json"
        if not meta_path.exists():
            print(f"feeds-probe: {recording['name']}: no recording; run with RECORD=1")
            differences += 1
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        body = (FIXTURES / f"{recording['name']}.body").read_bytes()
        age = (today - dt.datetime.fromisoformat(meta["captured_at"])).days
        live = _get(recording)
        try:
            recorded_shape, live_shape = _shape(json.loads(body)), _shape(live.json())
        except ValueError:
            recorded_shape, live_shape = body[:40], live.content[:40]
        same = live.status_code == meta["status"] and recorded_shape == live_shape
        deprecation = live.headers.get("deprecation")
        if deprecation != meta["headers"].get("deprecation", meta["headers"].get("Deprecation")):
            same = False
        verdict = "same shape" if same else "SHAPE DIFFERS"
        stale = f", older than {MAX_AGE_DAYS} days: re-record" if age > MAX_AGE_DAYS else ""
        print(
            f"feeds-probe: {recording['name']}: {verdict} (HTTP {meta['status']} recorded, "
            f"{live.status_code} live), recorded {age} day(s) ago{stale}"
        )
        differences += 0 if same else 1
    print(f"feeds-probe: {differences} difference(s) across {len(RECORDINGS)} recording(s)")
    return 1 if differences else 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--record", action="store_true")
    parser.add_argument("--only", action="append", help="record only this recording; repeatable")
    arguments = parser.parse_args(argv)
    return record(set(arguments.only or ())) if arguments.record else probe()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
