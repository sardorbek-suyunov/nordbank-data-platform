-- Sanctions list names (specification 009 section 4): every name and alias of every entity in
-- every landed list version, normalised by the macro merchants use. Matching is 010's.
with entities as (
    select * from {{ ref('br_opensanctions__entities') }}
)

{{ sanctions_names('entities') }}
