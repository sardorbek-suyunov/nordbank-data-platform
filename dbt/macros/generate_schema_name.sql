{# Custom schemas are used as written: bronze models live in `bronze`, not `main_bronze`. #}
{% macro generate_schema_name(custom_schema_name, node) -%}
  {{ custom_schema_name if custom_schema_name is not none else target.schema }}
{%- endmacro %}
