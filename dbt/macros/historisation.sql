{#
  How silver reads a relational entity's bronze (specification 008 sections 1 to 4).

  Bronze holds every extraction of every row: a key recurs once per batch that read it. Silver
  makes it one row per version (`scd2`) or one row per entity (`latest_state`).

  - **Deduplication.** A version is a key and its `updated_at`; the bronze rows sharing both are
    re-reads of it, and the earliest batch wins: the lowest `_batch_id`, which sorts by interval
    and then sequence. Planning measured no two re-reads that disagree, and
    `silver_rereads_agree` keeps it so.
  - **The version rule.** An observation opens a version when a column of the projection is
    distinct from the previous observation's, compared column by column with `is distinct from`
    so a null is a value; or when no contract column changed at all, which is a change the
    contract does not describe (the merchants case). A change confined to a measure the model
    excludes opens none, and the excluded measure is not carried. Never a hash: a collision would
    silently merge two real versions.
  - **The clocks.** The first version of a key opens at the epoch, 1900-01-01T00:00:00Z: its
    first observed state is backdated as its earliest known state, not its true state, because
    the historical load writes each entity's state as of the anchor and the reference seed is
    stamped with wall-clock time. Every later version opens at its `updated_at`. The current
    version closes at 9999-12-31T00:00:00Z, never null.
  - **Soft deletes.** An observation whose `deleted` expression is true opens nothing and closes
    the version before it at its `updated_at`, so the key has no current version and keeps its
    history; a later observation that is not deleted opens a version again. A `ref` table
    deactivates rather than deletes, and its model passes `deleted='false'`: an inactive row is a
    version like any other, current with `is_active` false.

  The projection is every column of the relation except the key, the audit columns, the deleted
  flag and the excluded measures. It is read from the relation when the model runs, so it cannot
  drift from the contract; a test passes `columns` explicitly, because its fixture is not a
  relation the adapter can describe.
#}

{% macro scd2_epoch() -%}timestamptz '1900-01-01 00:00:00+00'{%- endmacro %}
{% macro scd2_end() -%}timestamptz '9999-12-31 00:00:00+00'{%- endmacro %}

{#- Columns every bronze row carries that are not the entity's state. -#}
{% macro historisation_audit_columns() -%}
  {{ return(['created_at', 'updated_at', 'is_deleted', '_ingested_at', '_source_file', '_batch_id',
             '_source_system', '_object_batch_id', '_object_ingest_date', '_raw_payload']) }}
{%- endmacro %}

{% macro historisation_projection(relation, key, excluded, deleted, columns) -%}
  {%- if columns is none -%}
    {%- if not execute -%}
      {{ return([]) }}
    {%- endif -%}
    {%- set columns = adapter.get_columns_in_relation(relation) | map(attribute='name') | list -%}
  {%- endif -%}
  {%- set skipped = key + excluded + historisation_audit_columns() + [deleted] -%}
  {%- set projection = [] -%}
  {%- for column in columns -%}
    {%- if column not in skipped -%}
      {%- do projection.append(column) -%}
    {%- endif -%}
  {%- endfor -%}
  {%- if execute and projection | length == 0 -%}
    {{ exceptions.raise_compiler_error('historisation: ' ~ relation ~ ' has no column to compare') }}
  {%- endif -%}
  {{ return(projection) }}
{%- endmacro %}

{% macro historisation_observed(relation, key) -%}
    select distinct on ({{ key | join(', ') }}, updated_at) *
      from {{ relation }}
     order by {{ key | join(', ') }}, updated_at, _batch_id
{%- endmacro %}

{% macro scd2(relation, key, excluded=[], deleted='is_deleted', columns=none) -%}
{%- set projection = historisation_projection(relation, key, excluded, deleted, columns) -%}
{%- set k = key | join(', ') -%}
with observed as (
{{ historisation_observed(relation, key) }}
),

compared as (
    select *,
           ({{ deleted }}) as _deleted,
           lag(({{ deleted }})) over w as _previous_deleted,
           row_number() over w as _observation_number,
           ({%- for column in projection %}
            "{{ column }}" is distinct from lag("{{ column }}") over w{% if not loop.last %} or{% endif %}
           {%- endfor %}) as _projection_changed,
           ({%- for column in excluded %}
            "{{ column }}" is distinct from lag("{{ column }}") over w{% if not loop.last %} or{% endif %}
           {%- else %} false {%- endfor %}) as _excluded_changed
      from observed
    window w as (partition by {{ k }} order by updated_at)
),

opened as (
    select *,
           lead(updated_at) over (partition by {{ k }} order by updated_at) as _next_opened_at
      from compared
     where _observation_number = 1
        or _deleted is distinct from _previous_deleted
        or (not _deleted and (_projection_changed or not _excluded_changed))
)

select
    {%- for column in key + projection %}
    "{{ column }}",
    {%- endfor %}
    created_at,
    updated_at,
    _batch_id,
    case when row_number() over (partition by {{ k }} order by updated_at) = 1
         then {{ scd2_epoch() }} else updated_at end as _valid_from,
    coalesce(_next_opened_at, {{ scd2_end() }}) as _valid_to,
    _next_opened_at is null as _is_current
  from opened
 where not _deleted
{%- endmacro %}

{% macro latest_state(relation, key, deleted='is_deleted', columns=none) -%}
{%- set projection = historisation_projection(relation, key, [], deleted, columns) -%}
with observed as (
{{ historisation_observed(relation, key) }}
),

latest as (
    select distinct on ({{ key | join(', ') }}) *
      from observed
     order by {{ key | join(', ') }}, updated_at desc
)

select
    {%- for column in key + projection %}
    "{{ column }}",
    {%- endfor %}
    created_at,
    updated_at,
    _batch_id
  from latest
 where not ({{ deleted }})
{%- endmacro %}

{#
  Business validity (specification 008 section 3). A source that records when a fact is true
  with an inclusive last day, `customer_addresses` and `ref.interchange_rates`, is exposed
  half-open: from the first day to the day after the last, and to 9999-12-31 while open. A fact
  joins the version its date falls in, read from the current system-time version: a correction
  recorded later moves a past fact to the corrected row, which is the stated consequence of
  rejecting a bitemporal join.
#}
{% macro business_valid_to(valid_to_inclusive) -%}
coalesce({{ valid_to_inclusive }} + 1, date '9999-12-31')
{%- endmacro %}

{% macro business_valid_on(on_date, alias) -%}
({{ alias }}._is_current
 and {{ alias }}._business_valid_from_date <= {{ on_date }}
 and {{ on_date }} < {{ alias }}._business_valid_to_date)
{%- endmacro %}

{#- The names of a relation's columns, less those named in `skip`; empty when not executing. -#}
{% macro relation_columns(relation, skip=[]) -%}
  {%- set names = [] -%}
  {%- if execute -%}
    {%- for column in adapter.get_columns_in_relation(relation) -%}
      {%- if column.name not in skip -%}
        {%- do names.append(column.name) -%}
      {%- endif -%}
    {%- endfor -%}
  {%- endif -%}
  {{ return(names) }}
{%- endmacro %}
