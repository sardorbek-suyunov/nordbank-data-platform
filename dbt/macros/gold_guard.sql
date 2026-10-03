{#
  The gold column guard (specification 008 section 8): no gold model selects a column classified
  `quasi-identifier`. Silver keeps quasi-identifiers in the clear because generalising needs the
  underlying value; gold carries only the generalisation, an age band and never a date of birth.

  Checked by the `on-run-start` hook before any model or test runs, like `bronze_guard`, from the
  classifications rather than a hand list: every column a model's properties declare carries its
  classification in `meta`, generated from the contracts for the silver models that mirror an
  entity. For each gold model the guard reads the columns its parents classify
  `quasi-identifier` and fails the build if the model declares one of them. A gold model must
  enforce its contract, so what it declares is exactly what it returns: without that, a column
  could be selected and simply not declared.

  What it cannot see: a quasi-identifier selected under another name, `date_of_birth as born`.
  Column-level lineage would; a review does until then. `make dbt-prove` plants a gold model
  selecting a date of birth, and one that does not enforce its contract, and requires both to
  fail the build, and a model selecting only the band to pass.
#}
{% macro gold_guard() %}
  {%- if execute -%}
    {%- set problems = [] -%}
    {%- for node in graph.nodes.values() -%}
      {%- if node.resource_type == 'model' and node.fqn | length > 2 and node.fqn[1] == 'gold' -%}
        {%- if not node.config.contract.enforced -%}
          {%- do problems.append(node.name ~ ' does not enforce its contract') -%}
        {%- endif -%}
        {%- set quasi = {} -%}
        {%- for parent_id in node.depends_on.nodes -%}
          {%- set parent = graph.nodes.get(parent_id) -%}
          {%- if parent is not none -%}
            {%- for name, column in parent.columns.items() -%}
              {%- set meta = column.meta or {} -%}
              {%- set configured = (column.config or {}).get('meta', {}) if column.config is mapping else {} -%}
              {%- set classification = meta.get('classification') or configured.get('classification') -%}
              {%- if classification == 'quasi-identifier' -%}
                {%- do quasi.update({name: parent.name}) -%}
              {%- endif -%}
            {%- endfor -%}
          {%- endif -%}
        {%- endfor -%}
        {%- for name in node.columns -%}
          {%- if name in quasi -%}
            {%- do problems.append(node.name ~ ' selects ' ~ quasi[name] ~ '.' ~ name ~ ', a quasi-identifier') -%}
          {%- endif -%}
        {%- endfor -%}
      {%- endif -%}
    {%- endfor -%}
    {%- if problems -%}
      {{ exceptions.raise_compiler_error('gold_guard: ' ~ (problems | join('; '))) }}
    {%- endif -%}
  {%- endif -%}
{% endmacro %}
