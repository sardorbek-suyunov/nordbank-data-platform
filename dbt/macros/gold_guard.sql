{#
  The gold column guard (specification 008 section 8, amended 2026-10-04): no quasi-identifier
  reaches gold except through a declared generalisation. Silver keeps quasi-identifiers in the
  clear because generalising needs the underlying value; gold carries only what the declaration
  file `dbt/quasi_identifiers_in_gold.yml` permits, and the generalisations silver builds, such as
  an age band in place of a date of birth.

  Checked by the `on-run-start` hook before any model or test runs, like `bronze_guard`, from the
  classifications rather than a hand list: every column a model's properties declare carries its
  classification in `meta`, generated from the contracts for the silver models that mirror an
  entity, and a column the declaration file permits carries its generalisation there too, as
  `meta.gold_generalisation`, written by `make silver-generate`. For each gold model it fails:

  1. a model that does not enforce its contract, because then what it declares need not be what
     it returns;
  2. a declared column with no classification, so a column renamed on the way, `date_of_birth as
     born`, has to say what it is;
  3. a column classified `quasi-identifier` that no parent declares a generalisation for, whether
     it carries a parent's name or a new one;
  4. a column carrying the name of a parent's undeclared quasi-identifier, whatever it is
     classified as.

  What it still cannot see is a renamed quasi-identifier classified as something else. That needs
  column-level lineage, a known gap M8 owns. `make dbt-prove` proves each refusal on a planted
  gold model, and that a declared column and a generalisation pass.
#}
{% macro gold_guard() %}
  {%- if execute -%}
    {%- set classes = ['identifier', 'quasi-identifier', 'sensitive', 'pseudonymous_key', 'non-personal'] -%}
    {%- set problems = [] -%}
    {%- for node in graph.nodes.values() -%}
      {%- if node.resource_type == 'model' and node.fqn | length > 2 and node.fqn[1] == 'gold' -%}
        {%- if not node.config.contract.enforced -%}
          {%- do problems.append(node.name ~ ' does not enforce its contract') -%}
        {%- endif -%}
        {%- set undeclared = {} -%}
        {%- set declared = {} -%}
        {%- for parent_id in node.depends_on.nodes -%}
          {%- set parent = graph.nodes.get(parent_id) -%}
          {%- if parent is not none -%}
            {%- for name, column in parent.columns.items() -%}
              {%- set meta = column_meta(column) -%}
              {%- if meta.get('classification') == 'quasi-identifier' -%}
                {%- if meta.get('gold_generalisation') -%}
                  {%- do declared.update({name: parent.name}) -%}
                {%- else -%}
                  {%- do undeclared.update({name: parent.name}) -%}
                {%- endif -%}
              {%- endif -%}
            {%- endfor -%}
          {%- endif -%}
        {%- endfor -%}
        {%- for name, column in node.columns.items() -%}
          {%- set classification = column_meta(column).get('classification') -%}
          {%- if classification not in classes -%}
            {%- do problems.append(node.name ~ '.' ~ name ~ ' declares no classification') -%}
          {%- elif name in undeclared -%}
            {%- do problems.append(node.name ~ ' selects ' ~ undeclared[name] ~ '.' ~ name ~ ', a quasi-identifier with no declared generalisation') -%}
          {%- elif classification == 'quasi-identifier' and name not in declared -%}
            {%- do problems.append(node.name ~ '.' ~ name ~ ' is a quasi-identifier no parent declares a generalisation for') -%}
          {%- endif -%}
        {%- endfor -%}
      {%- endif -%}
    {%- endfor -%}
    {%- if problems -%}
      {{ exceptions.raise_compiler_error('gold_guard: ' ~ (problems | join('; '))) }}
    {%- endif -%}
  {%- endif -%}
{% endmacro %}

{#- A column's meta from the graph, wherever the properties put it: `meta` or `config.meta`. -#}
{% macro column_meta(column) -%}
  {%- set merged = {} -%}
  {%- if column.config is mapping and column.config.get('meta') -%}
    {%- do merged.update(column.config.get('meta')) -%}
  {%- endif -%}
  {%- if column.meta -%}
    {%- do merged.update(column.meta) -%}
  {%- endif -%}
  {{ return(merged) }}
{%- endmacro %}
