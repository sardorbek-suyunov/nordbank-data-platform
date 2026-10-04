{#
  The sanctions list's names, one row per entity, list version and name (specification 009
  section 4). An entity's `names` and `aliases` are the feed's multi-valued properties: every
  value of each becomes a row, with `name_type` saying which it came from, and the normalised
  representation beside the raw one, by the one name macro merchants use.

  The caption is not a name of its own: it is the publisher's display label, and measured on the
  history it equals the first name on every entity. A value the entity carries twice, or as both
  a name and an alias, is one row, typed `name`. Matching a name to a counterparty is 010's.

  `entities` is a relation or CTE name with the bronze columns.
#}
{% macro sanctions_names(entities) -%}
select
    entity_id,
    publisher_version,
    name_type,
    name,
    {{ normalise_name('name') }} as name_normalised,
    _batch_id
from (
    select
        entity_id,
        publisher_version,
        'name' as name_type,
        unnest(cast(names as varchar[])) as name,
        _batch_id
    from {{ entities }}
    union all
    select
        entity_id,
        publisher_version,
        'alias' as name_type,
        unnest(cast(aliases as varchar[])) as name,
        _batch_id
    from {{ entities }}
) as stated
where name is not null
qualify row_number() over (
    partition by entity_id, publisher_version, name
    order by name_type = 'alias', _batch_id
) = 1
{%- endmacro %}
