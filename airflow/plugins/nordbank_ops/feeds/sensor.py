"""Wait for the day's clearing file without holding a worker (spec 006 section 8).

The first thing in this platform that waits on something external. The sensor looks once; if
the file is there it returns at once, and if not it **defers**: the task gives up its worker
slot and a trigger in the triggerer polls the inbound prefix, handing back an event when the
file arrives or when the wait runs out. The worker is not occupied while nothing happens, which
is the whole difference between a deferrable sensor and a poking one.

**Running out of time is an answer, not a failure.** A processor that sends nothing for a day is
a correct outcome — the file may arrive late, three days on — so the timeout resolves the task
successfully with `found: false`, and the open step records empty batches for the day, as
specification 005 records an empty reference batch. The timeout is bounded: the freshness SLA
gives the settlement feed a four-hour grace, which is the default, and a run may shorten it.

The result says whether the task deferred, because a sensor that has never waited proves
nothing: the acceptance evidence asserts a run in which it did.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
from typing import Any

from airflow.sdk import BaseOperator
from airflow.triggers.base import BaseTrigger, TriggerEvent

DEFAULT_TIMEOUT_SECONDS = 4 * 60 * 60
DEFAULT_POKE_SECONDS = 5


def _first_key(conn_id: str, bucket: str, prefix: str) -> str | None:
    from nordbank_ops import clients

    response = clients.lake_client(conn_id).list_objects_v2(Bucket=bucket, Prefix=prefix)
    keys = sorted(item["Key"] for item in response.get("Contents", []))
    return keys[0] if keys else None


class InboundObjectTrigger(BaseTrigger):
    """Poll a prefix in the inbound bucket until an object appears or the deadline passes."""

    def __init__(
        self, conn_id: str, bucket: str, prefix: str, deadline: str, poke_seconds: float
    ) -> None:
        super().__init__()
        self.conn_id = conn_id
        self.bucket = bucket
        self.prefix = prefix
        self.deadline = deadline
        self.poke_seconds = poke_seconds

    def serialize(self) -> tuple[str, dict[str, Any]]:
        return (
            "nordbank_ops.feeds.sensor.InboundObjectTrigger",
            {
                "conn_id": self.conn_id,
                "bucket": self.bucket,
                "prefix": self.prefix,
                "deadline": self.deadline,
                "poke_seconds": self.poke_seconds,
            },
        )

    async def run(self):
        deadline = dt.datetime.fromisoformat(self.deadline)
        polls = 0
        while True:
            polls += 1
            key = await asyncio.to_thread(_first_key, self.conn_id, self.bucket, self.prefix)
            if key is not None:
                yield TriggerEvent({"found": True, "key": key, "polls": polls})
                return
            if dt.datetime.now(dt.UTC) >= deadline:
                yield TriggerEvent({"found": False, "key": None, "polls": polls})
                return
            await asyncio.sleep(self.poke_seconds)


class InboundFileSensor(BaseOperator):
    """Succeed when the day's file is in the inbound prefix, deferring while it is not."""

    def __init__(
        self, *, prefix_variable: str, pattern: str, conn_id: str = "nordbank_lake", **kwargs
    ) -> None:
        super().__init__(**kwargs)
        # Strings rather than a callable, so the operator serialises with the DAG: the prefix is
        # read from the environment and the day's part formatted from the pattern at run time.
        self.prefix_variable = prefix_variable
        self.pattern = pattern
        self.conn_id = conn_id

    def _settings(self, context) -> tuple[str, str, float, float]:
        conf = dict(getattr(context["dag_run"], "conf", None) or {})
        day = context["dag_run"].logical_date.date()
        timeout = float(conf.get("sensor_timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
        poke = float(conf.get("sensor_poke_seconds", DEFAULT_POKE_SECONDS))
        prefix = os.environ[self.prefix_variable] + self.pattern.format(day=day)
        return os.environ["INBOUND_BUCKET"], prefix, timeout, poke

    def execute(self, context):
        bucket, prefix, timeout, poke = self._settings(context)
        key = _first_key(self.conn_id, bucket, prefix)
        if key is not None:
            print(f"sensor: {bucket}/{key} is already there; not deferring")
            return {"found": True, "deferred": False, "key": key, "prefix": prefix}
        deadline = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=timeout)
        print(f"sensor: nothing under {bucket}/{prefix}; deferring until {deadline.isoformat()}")
        self.defer(
            trigger=InboundObjectTrigger(
                conn_id=self.conn_id,
                bucket=bucket,
                prefix=prefix,
                deadline=deadline.isoformat(),
                poke_seconds=poke,
            ),
            method_name="execute_complete",
        )

    def execute_complete(self, context, event=None):
        event = event or {}
        _bucket, prefix, _timeout, _poke = self._settings(context)
        if event.get("found"):
            print(f"sensor: resumed after deferral, {event['key']} arrived")
        else:
            print(f"sensor: resumed after deferral, no file under {prefix}; no file for this day")
        return {
            "found": bool(event.get("found")),
            "deferred": True,
            "key": event.get("key"),
            "polls": event.get("polls"),
            "prefix": prefix,
        }
