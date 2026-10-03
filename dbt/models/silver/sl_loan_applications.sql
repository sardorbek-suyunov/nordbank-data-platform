-- Loan applications, the latest state of each, a soft-deleted one absent (specification 008
-- sections 2 and 4). The risk band is the one assigned at decision, which Q8 reads, not the
-- customer's band today.
{{ latest_state(ref('br_corebank__loan_applications'), ['loan_application_id']) }}
