{#
  Two structural rules, checked by the `on-run-start` hook before any model or test runs, so a
  violation fails the build (spec 007 sections 3 and 5). `make dbt-prove` plants one violation
  of each and requires the build to fail.

  1. A bronze model is exactly its generated call to `bronze_read`, and no other model reads
     the lake: no `read_parquet`, `read_csv`, `glob` or `s3://` outside the macro. The macro is
     where the registered-batch filter lives, so a model that read the lake itself would read the
     output of runs that died.
  2. No test stores its failures. A stored failure copies the failing rows into a table, and a
     failing row of a bronze model can carry an identifier's token beside quasi-identifiers and
     sensitive values.

  The hook renders to an empty statement and holds no credential.
#}
{% macro bronze_guard() %}
  {%- if execute -%}
    {%- set problems = [] -%}
    {%- for node in graph.nodes.values() -%}
      {%- if node.resource_type == 'model' -%}
        {%- set code = node.raw_code -%}
        {%- if node.fqn | length > 2 and node.fqn[1] == 'bronze' -%}
          {%- set parts = node.name[3:].split('__', 1) -%}
          {%- set expected = "{{ bronze_read('" ~ parts[0] ~ "', '" ~ (parts[1] if parts | length > 1 else '') ~ "') }}" -%}
          {%- set body = [] -%}
          {%- for line in code.split('\n') -%}
            {%- if line.strip() and not line.strip().startswith('--') -%}
              {%- do body.append(line.strip()) -%}
            {%- endif -%}
          {%- endfor -%}
          {%- if not node.name.startswith('br_') or body != [expected] -%}
            {%- do problems.append(node.name ~ ' is not exactly ' ~ expected) -%}
          {%- endif -%}
        {%- else -%}
          {%- set lowered = code | lower -%}
          {%- for marker in ['read_parquet', 'read_csv', 'glob(', 's3://'] -%}
            {%- if marker in lowered -%}
              {%- do problems.append(node.name ~ ' reads the lake directly (' ~ marker ~ ')') -%}
            {%- endif -%}
          {%- endfor -%}
        {%- endif -%}
      {%- elif node.resource_type == 'test' and node.config.get('store_failures') -%}
        {%- do problems.append(node.name ~ ' sets store_failures') -%}
      {%- endif -%}
    {%- endfor -%}
    {%- if problems -%}
      {{ exceptions.raise_compiler_error('bronze_guard: ' ~ (problems | join('; '))) }}
    {%- endif -%}
  {%- endif -%}
{% endmacro %}

