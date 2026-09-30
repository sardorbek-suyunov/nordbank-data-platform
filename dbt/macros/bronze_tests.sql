{#
  The generic tests every bronze model carries (spec 007 section 5). Each is declared, with
  severity `error`, in the model's generated properties, and `make dbt-prove` shows each one
  failing on a planted fixture.

  A test returns the rows that violate it and nothing else. None selects a column value an
  identifier, a quasi-identifier or a sensitive column holds: a failure names batches and keys,
  and `store_failures` is never set (`bronze_guard`), so no failing row is copied anywhere.
#}

{# The grain: one row per key per batch. The key recurs across batches by design. #}
{% test bronze_unique_key(model, columns) %}
select {{ columns | join(', ') }}, count(*) as occurrences
  from {{ model }}
 group by {{ columns | join(', ') }}
having count(*) > 1
{% endtest %}

{# No null in any of the named columns. Returns the batch of each offending row. #}
{% test bronze_not_null(model, columns) %}
select _object_batch_id
  from {{ model }}
 where {% for column in columns -%}
       "{{ column }}" is null{% if not loop.last %} or {% endif %}
       {%- endfor %}
{% endtest %}

{#
  Landing reconciliation: for every registered batch of the entity, the model holds exactly the
  rows the registry says landed, a batch that landed nothing included. Counted by the object
  key's batch, which is what the filter keeps. A batch in the model that is not a registered
  batch of the entity is a failure too, and the filter makes it impossible.
#}
{% test bronze_landing_reconciliation(model, source_system, entity) %}
with registered as (
    select batch_id, rows_landed
      from {{ source('ops', 'batch_registry') }}
     where status = 'registered'
       and source_system = '{{ source_system }}' and entity = '{{ entity }}'
),
held as (
    select _object_batch_id as batch_id, count(*) as rows_held
      from {{ model }}
     group by 1
)
select r.batch_id, r.rows_landed, coalesce(h.rows_held, 0) as rows_held
  from registered r
  left join held h using (batch_id)
 where r.rows_landed <> coalesce(h.rows_held, 0)
union all
select h.batch_id, null, h.rows_held
  from held h
 where h.batch_id not in (select batch_id from registered)
{% endtest %}

{# An identifier column holds a token of the tokeniser's format, or null. #}
{% test bronze_token(model, column_name, width) %}
select _object_batch_id
  from {{ model }}
 where "{{ column_name }}" is not null
   and not regexp_full_match("{{ column_name }}", '[0-9a-f]{{ '{' ~ width ~ '}' }}')
{% endtest %}

{#
  The object key agrees with the file and with the registry: the key's `batch_id` is the
  record's `_batch_id`, and the key's `ingest_date` is the one the registry holds for the batch.
#}
{% test bronze_hive_key(model) %}
select m._object_batch_id, m._batch_id, m._object_ingest_date
  from {{ model }} m
  left join {{ source('ops', 'batch_registry') }} r on r.batch_id = m._object_batch_id
 where m._object_batch_id is distinct from m._batch_id
    or m._object_ingest_date is distinct from r.ingest_date
{% endtest %}
