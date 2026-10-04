{#
  A name in one representation (specification 009 section 4): upper case, accents removed,
  surrounding whitespace trimmed and runs of whitespace collapsed to one space. Representation
  only, and the one function every silver name passes through: merchants and the sanctions list's
  names and aliases alike, so a name normalised on one side compares with the other.

  Deciding that two different names are one party is matching, specification 010: the store
  numbers, legal forms and locations an acquirer appends are kept, and so is a letter with no
  decomposed form. `strip_accents` removes a combining mark, so `É` becomes `E` and `Å` becomes
  `A`, but `Ł` and `Ø` are letters of their own and stay as they are; transliterating them is a
  matching rule, not a representation.
#}
{% macro normalise_name(expression) -%}
regexp_replace(trim(upper(strip_accents({{ expression }}))), '\s+', ' ', 'g')
{%- endmacro %}
