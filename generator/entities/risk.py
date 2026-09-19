"""Fraud alerts and login sessions.

Both are written by the movement pass rather than by a pass of their own, because both are
about individual transactions and the transaction ids only exist there.

Alerts fire on some legitimate transactions and miss some fraud. That is the point: a detector
that fired on exactly the fraudulent transactions would make Q10's precision 100 percent and
its false positive rate zero, and the mart would be measuring the generator instead of a fraud
operation.

Sessions come in two kinds. Background sessions are the customer opening the app without
transacting. Linked sessions precede a digital transaction, at the rate spec 003's replacement
for invariant 13 asserts: a stated minimum share of customer-initiated digital-channel
transactions are preceded by a login within 24 hours. Card-present and recurring transactions
are excluded from that rule, since neither implies a login.
"""

from __future__ import annotations

import datetime as dt
import random

from ..realism import fraud as fraud_model
from ..realism import lifecycle
from ..realism.distributions import bernoulli
from ..spool import Spool


class AlertWriter:
    """Assigns alert ids and writes fraud alert rows."""

    __slots__ = ("_next_id", "_rows", "confirmed", "dismissed", "open", "raised")

    def __init__(self, spool: Spool) -> None:
        self._rows = spool.table("fraud_alerts")
        self._next_id = 0
        self.raised = 0
        self.confirmed = 0
        self.dismissed = 0
        self.open = 0

    def consider(
        self,
        rng: random.Random,
        fraud_params: dict,
        transaction_id: int,
        customer_id: int,
        booked_at: dt.datetime,
        fraudulent: bool,
        context: fraud_model.FraudContext,
        anchor_end: dt.datetime,
    ) -> None:
        if not fraud_model.raises_alert(rng, fraud_params, fraudulent):
            return

        self._next_id += 1
        self.raised += 1
        alert_id = self._next_id
        # The alert fires minutes to hours after the transaction, never before it.
        alerted_at = booked_at + dt.timedelta(minutes=rng.randrange(1, 240))
        if alerted_at > anchor_end:
            alerted_at = anchor_end

        disposition, dispositioned_at = fraud_model.disposition(
            rng, fraud_params, alerted_at, anchor_end, fraudulent
        )
        if disposition == "confirmed_fraud":
            self.confirmed += 1
        elif disposition == "dismissed":
            self.dismissed += 1
        else:
            self.open += 1

        analyst = (
            f"ANL{rng.randrange(1, 240):05d}"
            if bernoulli(rng, float(fraud_params["analyst_reference_share"]))
            else None
        )

        self._rows.write(
            (
                alert_id,
                f"ALR{alert_id:010d}",
                transaction_id,
                customer_id,
                fraud_model.rule_for(rng, fraud_params, context),
                disposition,
                alerted_at,
                dispositioned_at,
                round(fraud_model.alert_score(rng, fraud_params, fraudulent), 8),
                analyst,
                alerted_at,
                dispositioned_at or alerted_at,
                False,
            )
        )


class SessionWriter:
    """Assigns session ids and writes login session rows."""

    __slots__ = ("_countries", "_next_id", "_rows", "_seed", "linked", "total")

    def __init__(self, spool: Spool, seed: int, countries: list[str]) -> None:
        self._rows = spool.table("login_sessions")
        self._next_id = 0
        self._seed = seed
        self._countries = countries
        self.total = 0
        self.linked = 0

    def write(
        self,
        rng: random.Random,
        digital: dict,
        customer_id: int,
        started_at: dt.datetime,
        device_index: int,
        residence_country: str,
        anchor_end: dt.datetime,
        linked: bool = False,
    ) -> None:
        if started_at > anchor_end:
            return
        self._next_id += 1
        self.total += 1
        if linked:
            self.linked += 1
        session_id = self._next_id

        minutes = min(
            600.0,
            max(
                0.5,
                2.718281828
                ** rng.gauss(
                    float(digital["session_minutes_mu"]), float(digital["session_minutes_sigma"])
                ),
            ),
        )
        ended_at: dt.datetime | None = started_at + dt.timedelta(minutes=minutes)
        if bernoulli(rng, float(digital["unclosed_share"])) or ended_at > anchor_end:
            ended_at = None

        # Usually the customer is where they live. Travel and VPNs are the rest, and they are
        # what gives Q19 a geography signal rather than a constant.
        if bernoulli(rng, float(digital["ip_country_null_share"])):
            ip_country = None
        elif bernoulli(rng, float(digital["ip_country_matches_residence"])):
            ip_country = residence_country
        else:
            ip_country = self._countries[rng.randrange(len(self._countries))]

        self._rows.write(
            (
                session_id,
                f"SES{session_id:012d}",
                customer_id,
                lifecycle.session_channel(rng, digital),
                lifecycle.session_outcome(rng, digital),
                started_at,
                ended_at,
                lifecycle.device_fingerprint(self._seed, customer_id, device_index),
                lifecycle.ip_address(self._seed, customer_id, session_id),
                ip_country,
                started_at,
                ended_at or started_at,
                False,
            )
        )
