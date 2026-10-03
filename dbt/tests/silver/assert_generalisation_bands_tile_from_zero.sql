-- The band edges tile every kind from zero with no gap or overlap, and end with one open band
-- (specification 008 section 8). The band models are contiguous only because the edges are.
-- Returns each band that breaks the tiling.
with ordered as (
    select
        band_kind,
        band,
        lower_years,
        upper_years,
        lag(upper_years) over w as previous_upper,
        lead(lower_years) over w as next_lower
    from {{ ref('seed_generalisation_bands') }}
    window w as (partition by band_kind order by lower_years)
)

select
    band_kind,
    band
from ordered
where
    (previous_upper is null and lower_years <> 0)
    or (previous_upper is not null and lower_years <> previous_upper)
    or (upper_years is not null and upper_years <= lower_years)
    or (upper_years is null and next_lower is not null)
    or (upper_years is not null and next_lower is null)
