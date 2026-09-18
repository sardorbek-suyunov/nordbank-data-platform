-- Underwriting bands, decision reasons and fraud detection rules.

-- Bands are half-open intervals: a probability of default of exactly 0.0050 falls in band B.
-- mart_credit_underwriting (Q8) orders by band_ordinal and compares the realised default rate
-- against the range the band was written for.
insert into ref.risk_bands (code, name, band_ordinal, pd_lower_bound, pd_upper_bound, description, is_active) values
    ('A', 'Band A, lowest risk',  1, 0.00000000, 0.00500000, null, true),
    ('B', 'Band B',               2, 0.00500000, 0.01500000, null, true),
    ('C', 'Band C',               3, 0.01500000, 0.04000000, null, true),
    ('D', 'Band D',               4, 0.04000000, 0.10000000, null, true),
    ('E', 'Band E, highest risk', 5, 0.10000000, 1.00000000, null, true)
on conflict (code) do update
    set name = excluded.name, band_ordinal = excluded.band_ordinal,
        pd_lower_bound = excluded.pd_lower_bound, pd_upper_bound = excluded.pd_upper_bound,
        description = excluded.description, is_active = excluded.is_active
  where (ref.risk_bands.name, ref.risk_bands.band_ordinal, ref.risk_bands.pd_lower_bound,
         ref.risk_bands.pd_upper_bound, ref.risk_bands.description, ref.risk_bands.is_active)
        is distinct from (excluded.name, excluded.band_ordinal, excluded.pd_lower_bound,
                          excluded.pd_upper_bound, excluded.description, excluded.is_active);

-- An open vocabulary: this is the starting set, and the M3 generator adds to it as a seed row
-- rather than as a schema migration. That is the whole reason this is a table.
insert into ref.decision_reasons (code, name, description, is_active) values
    ('within_policy',       'Within policy',                'Approved with no exception', true),
    ('affordability',       'Affordability',                null, true),
    ('credit_history',      'Credit history',               null, true),
    ('existing_arrears',    'Existing arrears',             null, true),
    ('fraud_suspicion',     'Fraud suspicion',              null, true),
    ('documentation',       'Incomplete documentation',     null, true),
    ('policy_exclusion',    'Policy exclusion',             null, true),
    ('customer_withdrew',   'Withdrawn by the customer',    null, true),
    ('expired_no_response', 'Expired without a response',   null, true)
on conflict (code) do update
    set name = excluded.name, description = excluded.description, is_active = excluded.is_active
  where (ref.decision_reasons.name, ref.decision_reasons.description,
         ref.decision_reasons.is_active)
        is distinct from (excluded.name, excluded.description, excluded.is_active);

-- Q10 groups alert precision by rule, so a rule is an entity with its own row rather than a
-- string on an alert.
insert into ref.fraud_rules (code, name, description, is_active) values
    ('velocity_card',        'Card transaction velocity',        'More card transactions in a short window than the customer''s own baseline', true),
    ('geo_impossible',       'Impossible travel',                'Two card present transactions too far apart to be the same person', true),
    ('high_value_cnp',       'High value card not present',      null, true),
    ('new_device_high_value', 'High value from a new device',    'Pairs with the unrecognised device measure in Q19', true),
    ('structuring_cash',     'Sub-threshold cash deposits',      'Pairs with the structuring candidate list in Q11', true),
    ('dormant_reactivation', 'Activity on a dormant account',    null, true),
    ('merchant_risk',        'High risk merchant category',      null, true)
on conflict (code) do update
    set name = excluded.name, description = excluded.description, is_active = excluded.is_active
  where (ref.fraud_rules.name, ref.fraud_rules.description, ref.fraud_rules.is_active)
        is distinct from (excluded.name, excluded.description, excluded.is_active);
