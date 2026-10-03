-- Fraud alerts, the latest state of each, a soft-deleted one absent (specification 008 sections
-- 2 and 4): the disposition an analyst reached, which Q10's precision reads.
{{ latest_state(ref('br_corebank__fraud_alerts'), ['fraud_alert_id']) }}
