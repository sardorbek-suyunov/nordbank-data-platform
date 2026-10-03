-- Loans, one row per version (specification 008 section 2). A loan carries its own
-- `nominal_annual_rate`, copied from the product at disbursement, and Q5 reads it, not the
-- product's; planning measured no rate change in the history.
{{ scd2(ref('br_corebank__loans'), ['loan_id']) }}
