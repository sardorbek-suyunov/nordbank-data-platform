"""Publish one day's sanctions list: `python -m generator.sanctions --date D`.

Writes the day's publication under `opensanctions/sanctions/<version>/` in the inbound bucket and
points `opensanctions/sanctions/latest/index.json` at it, as the real publisher's `latest` path
does. Idempotent: the same day publishes the same bytes under the same version.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
for candidate in (ROOT, ROOT / "scripts"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from generator.config import load_profile  # noqa: E402
from generator.sanctions import build  # noqa: E402
from generator.settlement.__main__ import inbound_client  # noqa: E402

PREFIX = "opensanctions/sanctions/"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m generator.sanctions")
    parser.add_argument("--date", required=True)
    arguments = parser.parse_args(argv)
    day = dt.date.fromisoformat(arguments.date)

    from source_db_driver import connect

    with connect() as connection, connection.cursor() as cursor:
        cursor.execute("select profile, seed, anchor_date from platform.simulation_state")
        state = cursor.fetchone()
    if state is None:
        raise SystemExit("sanctions: the source has no simulation state; run `make seed`")
    profile, seed, anchor = state
    parameters = build.Parameters.from_profile(load_profile(profile).section("sanctions_list"))
    publication = build.publish(int(seed), anchor, day, parameters)

    client, bucket = inbound_client()
    base = f"{PREFIX}{publication.version}/"
    client.put_object(Bucket=bucket, Key=base + "entities.ftm.json", Body=publication.entities)
    client.put_object(Bucket=bucket, Key=base + "index.json", Body=publication.index)
    client.put_object(Bucket=bucket, Key=PREFIX + "latest/index.json", Body=publication.index)
    print(
        f"sanctions: {day}: version {publication.version}, {publication.entity_count} "
        f"entities, {len(publication.entities)} bytes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
