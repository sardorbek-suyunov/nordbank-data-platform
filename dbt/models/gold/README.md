# dbt/models/gold

Gold models, named `dim_<entity>`, `fct_<grain>` and `mart_<domain>_<subject>`.

The dimensional model and the marts that answer `docs/business_questions.md`. Each mart
declares its grain in its schema file and maps to the numbered questions it serves.

The layer stays BI-tool agnostic: no shaping that exists only to suit Power BI or Streamlit
(ADR 0004).

Populated from M6.
