-- Sanctions list entities (specification 009 section 4): one row per entity per landed list
-- version. One batch is one whole list (ADR 0015) and a snapshot the platform recognised as a
-- no-op landed no batch, so every bronze row is an entity of a version, once. The feed's
-- multi-valued properties stay lists, and the partial birth dates stay text.
select
    entity_id,
    publisher_version,
    published_at,
    entity_schema,
    caption,
    cast(datasets as varchar[]) as datasets,
    cast(referents as varchar[]) as referents,
    cast(topics as varchar[]) as topics,
    cast(countries as varchar[]) as countries,
    cast(nationalities as varchar[]) as nationalities,
    cast(birth_dates as varchar[]) as birth_dates,
    first_seen,
    last_change,
    _batch_id
from {{ ref('br_opensanctions__entities') }}
