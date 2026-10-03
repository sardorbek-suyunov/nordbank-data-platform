-- Customers, one row per version (specification 008 section 2). Every contract column is in the
-- projection: no measure here changes without a question reading its history. A soft-deleted
-- customer keeps its versions and has no current one.
{{ scd2(ref('br_corebank__customers'), ['customer_id']) }}
