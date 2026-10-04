-- Macro indicators (specification 009 section 5): each series and period's values by the
-- interval the platform observed them, runs of identical values collapsed. The feed has never
-- landed, because this deployment holds no FRED key, so the model builds typed and empty, and
-- its rules are proven on fixtures with a revision (`make dbt-prove`).
select * from (
    {{ observed_intervals(ref('br_fred__series')) }}
) as intervals
