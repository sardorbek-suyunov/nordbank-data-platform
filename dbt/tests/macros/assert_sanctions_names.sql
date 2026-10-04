-- The sanctions list's names (specification 009 section 4), on an entity the synthetic list never
-- has: two names and two aliases, accented and in mixed case, one alias repeating a name and one
-- repeated within the aliases; an entity with no alias; and an entity in two list versions. The
-- history's every multi-valued property holds at most one value and every name is already
-- normalised, so this is where several names and their normalisation are shown. Returns every
-- name row that differs from the expected.
with entities (entity_id, publisher_version, names, aliases, _batch_id) as (
    values
    (
        'NK-1', 'v1',
        '["ZZ-Société Générale Fixture", "ZZ-Societe  Generale Fixture"]'::json,
        '["ZZ-Øresund Trading", "ZZ-Société Générale Fixture", "ZZ-Øresund Trading"]'::json,
        'b-1'
    ),
    ('NK-2', 'v1', '["ZZ-SYNTHETIC ALPHA 0042 SANCTIONS-FIXTURE"]'::json, null::json, 'b-1'),
    ('NK-2', 'v2', '["ZZ-SYNTHETIC ALPHA 0042 SANCTIONS-FIXTURE"]'::json, null::json, 'b-2')
),

expected (entity_id, publisher_version, name_type, name, name_normalised, _batch_id) as (
    values
    ('NK-1', 'v1', 'name', 'ZZ-Société Générale Fixture', 'ZZ-SOCIETE GENERALE FIXTURE', 'b-1'),
    ('NK-1', 'v1', 'name', 'ZZ-Societe  Generale Fixture', 'ZZ-SOCIETE GENERALE FIXTURE', 'b-1'),
    ('NK-1', 'v1', 'alias', 'ZZ-Øresund Trading', 'ZZ-ØRESUND TRADING', 'b-1'),
    (
        'NK-2', 'v1', 'name', 'ZZ-SYNTHETIC ALPHA 0042 SANCTIONS-FIXTURE',
        'ZZ-SYNTHETIC ALPHA 0042 SANCTIONS-FIXTURE', 'b-1'
    ),
    (
        'NK-2', 'v2', 'name', 'ZZ-SYNTHETIC ALPHA 0042 SANCTIONS-FIXTURE',
        'ZZ-SYNTHETIC ALPHA 0042 SANCTIONS-FIXTURE', 'b-2'
    )
),

produced as (
    {{ sanctions_names('entities') }}
),

missing as (
    select * from expected
    except all
    select * from produced
),

unexpected as (
    select * from produced
    except all
    select * from expected
)

select
    'missing' as problem,
    entity_id,
    publisher_version,
    name_type
from missing
union all
select
    'unexpected' as problem,
    entity_id,
    publisher_version,
    name_type
from unexpected
