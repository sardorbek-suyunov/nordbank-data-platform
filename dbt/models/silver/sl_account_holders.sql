-- Who holds each account, one row per version (specification 008 section 2). Planning measured
-- no holding change in the history; the model is SCD2 because a holder's role or ownership
-- weight can change, and Q3, Q6 and Q11 read who held an account when.
{{ scd2(ref('br_corebank__account_holders'), ['account_holder_id']) }}
