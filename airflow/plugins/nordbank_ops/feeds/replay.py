"""Serve recorded responses instead of calling a publisher (spec 007 section 8).

The `stack` workflow ingests a week through the real DAGs, and the FX feed is the one feed that
calls a third party's API. CI must make no live external call, so when `FEED_FIXTURE_DIR` names
a directory of recordings, the interval feeds' fetch goes to it instead of the network: every
request is answered with the recorded response whose endpoint path and query match it, through
the same retry policy, parser and landing rule as a live one. The recordings are the ones
`make feeds-probe` writes and compares with the live API (`scripts/feeds_probe.py`).

A request with no recording raises, which fails the extract task and so the batch; it is never
answered with an absence, because a missing recording is a defect in the test setup rather than
a date the publisher did not publish. CI also points the live URL at an unroutable address, so a
fetch that somehow bypassed this would fail loudly rather than reach the internet.

Unset, which is every deployment and every local run, the feeds call the publisher.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

VARIABLE = "FEED_FIXTURE_DIR"


class NoRecordingError(RuntimeError):
    """A request the fixture directory holds no recording for."""


@dataclass(frozen=True)
class RecordedResponse:
    status_code: int
    content: bytes
    headers: dict = field(default_factory=dict)


def _key(url: str, params: dict | None) -> tuple[str, tuple]:
    return urlparse(url).path.rstrip("/"), tuple(sorted((params or {}).items()))


class Replay:
    """A `requests.get` stand-in over a directory of `<name>.body` and `<name>.meta.json`."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.index: dict[tuple[str, tuple], Path] = {}
        for meta in sorted(directory.glob("*.meta.json")):
            recorded = json.loads(meta.read_text(encoding="utf-8"))
            self.index[_key(recorded["endpoint"], recorded.get("params"))] = meta
        if not self.index:
            raise NoRecordingError(f"{VARIABLE} names {directory}, which holds no recording")

    def __call__(self, url: str, params: dict | None = None, timeout: float | None = None):
        meta = self.index.get(_key(url, params))
        if meta is None:
            raise NoRecordingError(
                f"no recorded response for {urlparse(url).path} with {params or {}} in "
                f"{self.directory}; record it with `make feeds-probe`"
            )
        recorded = json.loads(meta.read_text(encoding="utf-8"))
        body = meta.with_name(meta.name.removesuffix(".meta.json") + ".body").read_bytes()
        return RecordedResponse(recorded["status"], body, recorded.get("headers", {}))


def from_environment(environ: dict | None = None) -> Replay | None:
    raw = (os.environ if environ is None else environ).get(VARIABLE, "").strip()
    return Replay(Path(raw)) if raw else None
