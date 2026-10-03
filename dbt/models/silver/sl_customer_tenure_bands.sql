-- The tenure band each customer is in, as half-open date intervals from the signup date
-- (specification 008 section 8): the generalisation of a quasi-identifier that may reach gold.
{{ customer_bands('tenure', 'signup_date', 'tenure_band') }}
