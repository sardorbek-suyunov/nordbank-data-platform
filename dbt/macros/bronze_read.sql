{#
  The one way a bronze model reads the lake (spec 007 section 3, ADR 0018).

  Every bronze model is a single call to this macro, generated from its contract, and
  `bronze_guard` fails the build if a model reads the lake any other way.

  What it does, and the measurement behind each part:

  - Reads every object under the entity's prefix **by column name** (`union_by_name`). Objects
    written under different contract versions carry different columns, and objects written
    before the writer applied the contract's schema carry different types for the same column.
    Measured on DuckDB 1.5.5: without `union_by_name` the first file's schema wins, a later file
    missing a column fails the read, and a column absent from the first file is silently
    dropped; four of the thirteen entities with more than one physical schema failed outright.
  - Casts **every column** to the type the model's properties declare, which the generator took
    from the latest contract version carrying it. A column no object carries yet is selected as
    a typed null, decided when the view is built.
  - Keeps only rows whose **object key's `batch_id`** is registered in `ops.batch_registry`. The
    file's own `_batch_id` column is not the filter: measured, a copy of a registered file
    planted under a new key passes a filter on it.
  - An entity with **no registered batch with rows landed** builds as a typed empty relation.
    A registered empty batch writes no object, and a glob that matches nothing fails when the
    view is created and whenever it is queried, with no fallback in SQL. That decision is made
    when the view is built, so the view goes stale once, when the entity's first rows land;
    `transform_bronze` is scheduled on every ingestion's registrations, so its next run
    rebuilds it.

  A view re-lists the prefix on every query, so a new batch is visible without a rebuild.
#}
{% macro bronze_read(source_system, entity) %}
  {%- set registry = source('ops', 'batch_registry') -%}
  {%- set key_columns = ['_object_batch_id', '_object_ingest_date'] -%}
  {%- set columns = [] -%}
  {%- for column in model.columns.values() -%}
    {%- if column.name not in key_columns -%}
      {%- do columns.append(column) -%}
    {%- endif -%}
  {%- endfor -%}
  {#- The properties are attached after parsing, so the columns exist only when executing. -#}
  {%- if execute and columns | length == 0 -%}
    {{ exceptions.raise_compiler_error(model.name ~ ' declares no columns; regenerate it') }}
  {%- endif -%}
  {%- set lake = var('lake_root', 's3://' ~ env_var('LAKE_BUCKET', 'unset')) -%}
  {%- set objects = lake ~ '/bronze/' ~ source_system ~ '/' ~ entity ~ '/*/*/*.parquet' -%}
  {%- set scan -%}
    read_parquet('{{ objects }}', union_by_name = true, hive_partitioning = true,
                 hive_types = {'ingest_date': DATE, 'batch_id': VARCHAR})
  {%- endset -%}

  {%- set landed = true -%}
  {%- set present = [] -%}
  {%- if execute -%}
    {%- set count_sql -%}
      select count(*) from {{ registry }}
       where status = 'registered' and rows_landed > 0
         and source_system = '{{ source_system }}' and entity = '{{ entity }}'
    {%- endset -%}
    {%- set landed = run_query(count_sql).columns[0].values()[0] > 0 -%}
    {%- if landed -%}
      {%- set described = run_query('describe select * from ' ~ scan) -%}
      {%- for name in described.columns[0].values() -%}
        {%- do present.append(name) -%}
      {%- endfor -%}
    {%- endif -%}
  {%- endif -%}

  {%- if landed %}
select
  {%- for column in columns %}
    {% if not execute or column.name in present -%}
      cast(b."{{ column.name }}" as {{ column.data_type }})
    {%- else -%}
      cast(null as {{ column.data_type }})
    {%- endif %} as "{{ column.name }}",
  {%- endfor %}
    b.batch_id as _object_batch_id,
    b.ingest_date as _object_ingest_date
from {{ scan }} as b
where b.batch_id in (
    select batch_id from {{ registry }}
     where status = 'registered'
       and source_system = '{{ source_system }}' and entity = '{{ entity }}'
)
  {%- else %}
-- No registered batch of {{ source_system }}.{{ entity }} has landed rows, so no object exists to
-- read; the relation is empty and typed until the first one does.
select
  {%- for column in columns %}
    cast(null as {{ column.data_type }}) as "{{ column.name }}",
  {%- endfor %}
    cast(null as varchar) as _object_batch_id,
    cast(null as date) as _object_ingest_date
where false
  {%- endif %}
{% endmacro %}
