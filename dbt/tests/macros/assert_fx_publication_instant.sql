-- A rate is published at 16:00 Europe/Berlin on its date: 15:00 UTC in winter and 14:00 UTC
-- in summer, across both daylight-saving changes and on the days of the changes themselves
-- (specification 008 section 6). In 2026 summer time begins on 29 March and ends on 25 October.
with cases (rate_date, expected) as (
    values
    (date '2026-03-27', timestamptz '2026-03-27 15:00:00+00'),
    (date '2026-03-29', timestamptz '2026-03-29 14:00:00+00'),
    (date '2026-03-30', timestamptz '2026-03-30 14:00:00+00'),
    (date '2026-07-20', timestamptz '2026-07-20 14:00:00+00'),
    (date '2026-10-23', timestamptz '2026-10-23 14:00:00+00'),
    (date '2026-10-25', timestamptz '2026-10-25 15:00:00+00'),
    (date '2026-10-26', timestamptz '2026-10-26 15:00:00+00'),
    (date '2024-01-23', timestamptz '2024-01-23 15:00:00+00')
)

select
    rate_date,
    expected,
    {{ fx_publication_instant('rate_date') }} as computed
from cases
where {{ fx_publication_instant('rate_date') }} is distinct from expected
