-- Cards, one row per version (specification 008 section 2): every status change, issue and
-- replacement is a version.
{{ scd2(ref('br_corebank__cards'), ['card_id']) }}
