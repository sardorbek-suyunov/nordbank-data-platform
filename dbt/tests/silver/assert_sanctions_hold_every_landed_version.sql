-- The sanctions models hold every entity and name of every landed list version (specification
-- 009 section 4, criterion 5), restated from bronze without the names macro: per version, the
-- entities bronze landed and the distinct values of their `names` and `aliases`, read with the
-- JSON path functions rather than a cast. Returns each version whose counts differ, a version
-- silver lacks included.
with landed as (
    select
        publisher_version,
        count(*) as entities
    from {{ ref('br_opensanctions__entities') }}
    group by publisher_version
),

stated as (
    select
        entity_id,
        publisher_version,
        unnest(json_extract_string(names, '$[*]')) as stated_name
    from {{ ref('br_opensanctions__entities') }}
    union
    select
        entity_id,
        publisher_version,
        unnest(json_extract_string(aliases, '$[*]')) as stated_name
    from {{ ref('br_opensanctions__entities') }}
),

landed_names as (
    select
        publisher_version,
        count(*) as names_stated
    from stated
    where stated_name is not null
    group by publisher_version
),

held as (
    select
        publisher_version,
        count(*) as entities
    from {{ ref('sl_sanctions_entities') }}
    group by publisher_version
),

held_names as (
    select
        publisher_version,
        count(*) as names_stated
    from {{ ref('sl_sanctions_names') }}
    group by publisher_version
)

select
    landed.publisher_version,
    landed.entities as entities_landed,
    held.entities as entities_held,
    landed_names.names_stated as names_landed,
    held_names.names_stated as names_held
from landed
left join landed_names on landed.publisher_version = landed_names.publisher_version
left join held on landed.publisher_version = held.publisher_version
left join held_names on landed.publisher_version = held_names.publisher_version
where
    held.entities is distinct from landed.entities
    or held_names.names_stated is distinct from landed_names.names_stated
