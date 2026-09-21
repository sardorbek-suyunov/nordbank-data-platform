"""Fraud alerts reaching a verdict, days after they fired.

**The lag is the point of this phase, not a detail of it.** `metric_definitions.md` attributes
alert precision to the disposition month rather than the alert month, and the reason that rule
exists is that dispositions arrive later than the alerts they belong to. A generator that
dispositioned an alert the moment it raised one would leave that rule with nothing to be right
about. So an alert raised on day D is finalised between D+1 and D+14, on the distribution
`generator/profiles.yml` already states for the historical load.

**The lag is drawn from a stream keyed on the alert**, so it is the same number on every replay
and does not have to be stored anywhere. Nothing in the schema records when an analyst intends
to look at a case, and adding a column for it would put a simulation detail into the bank's
data.

**The verdict follows the score, not the truth.** A tick reads an alert out of the database and
cannot know whether the transaction behind it was fraudulent — nothing in `core` records that,
and nothing should, because a bank does not store which of its transactions were really fraud.
It stores which alerts its analysts confirmed. `generator/realism/fraud.py` turns the alert
score into the posterior probability of fraud against the detector's own prior, which
reproduces the historical precision by construction because the scores were drawn from the same
two distributions.
"""

from __future__ import annotations

import datetime as dt

from ...realism import fraud as fraud_model
from ...realism.distributions import bernoulli
from ..tick import TickContext
from ..writer import apply_updates

CONFIRMED = "confirmed_fraud"
DISMISSED = "dismissed"


def run(context: TickContext) -> None:
    params = context.profile.params["fraud"]
    lag_min = int(params["disposition_lag_days_min"])
    lag_max = int(params["disposition_lag_days_max"])
    day_start, _ = context.window

    context.cursor.execute(
        """
        select f.fraud_alert_id, f.alerted_at, f.alert_score
          from core.fraud_alerts f
          join ref.fraud_dispositions d on d.code = f.fraud_disposition_code
         where not d.is_final and not f.is_deleted
           and f.alerted_at < %s
         order by f.fraud_alert_id
        """,
        (day_start,),
    )
    candidates = context.cursor.fetchall()
    if not candidates:
        return

    day_end = context.at(23, 59, 59)
    confirmed: set[int] = set()
    dismissed: set[int] = set()

    for alert_id, alerted_at, score in candidates:
        # Keyed on the alert, not on the day: a lag redrawn each tick is realised as
        # the minimum over repeated draws. See `TickContext.entity_stream`.
        rng = context.entity_stream("disposition", alert_id)
        # Due today or overdue. Overdue covers the backlog the historical load left open at the
        # anchor, which a simulation that never worked it would carry for ever.
        if alerted_at + dt.timedelta(days=rng.randint(lag_min, lag_max)) > day_end:
            continue
        if bernoulli(rng, fraud_model.confirmation_probability(params, float(score))):
            confirmed.add(alert_id)
        else:
            dismissed.add(alert_id)

    due = sorted(confirmed | dismissed)
    if not due:
        return

    # Stamped inside the phase's own window, so the verdict lands in an analyst's working
    # afternoon rather than at whatever instant the lag arithmetic produced. The measured lag is
    # therefore the difference between two dates in the data, which is what criterion 7 reads.
    for instant, group in context.jitter_groups("dispositions", due, key_of=lambda alert: alert):
        context.set_clock(instant)
        for disposition, population in ((CONFIRMED, confirmed), (DISMISSED, dismissed)):
            context.report.record_update(
                "fraud_alerts",
                apply_updates(
                    context.cursor,
                    "update core.fraud_alerts set fraud_disposition_code = %s, "
                    "dispositioned_at = %s where fraud_alert_id = any(%s::bigint[]) "
                    "returning fraud_alert_id",
                    [alert_id for alert_id in group if alert_id in population],
                    scalars=(disposition, instant),
                ),
            )
