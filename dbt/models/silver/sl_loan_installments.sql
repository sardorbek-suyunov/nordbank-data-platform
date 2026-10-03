-- Loan installments, one row per version (specification 008 section 2). `paid_amount` stays in
-- the projection, so every payment against an installment is a version: Q7 reads when an
-- installment was paid, and how much, as of each month end.
{{ scd2(ref('br_corebank__loan_installments'), ['loan_installment_id']) }}
