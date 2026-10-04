-- `normalise_name` changes representation only (specification 009 section 4): case, accents
-- and whitespace, and nothing a matcher would decide. The history's merchant and sanctions
-- names are already upper case, unaccented and single-spaced, so the macro's effect is shown
-- here, on names with diacritics, mixed case, runs of spaces, and a tab and a line feed at the
-- edges. A letter with no decomposed form, `Ł`, is kept, and a null stays null. Returns every
-- case the macro gets wrong.
with cases (raw, expected) as (
    values
    ('Société  Générale', 'SOCIETE GENERALE'),
    ('  müller   gmbh ', 'MULLER GMBH'),
    ('Ålborg Havn', 'ALBORG HAVN'),
    ('ZZ-Synthétique Ñandú 0042 sanctions-fixture', 'ZZ-SYNTHETIQUE NANDU 0042 SANCTIONS-FIXTURE'),
    ('Łódź Trading', 'ŁODZ TRADING'),
    (chr(9) || 'Acme  Retail' || chr(10), 'ACME RETAIL'),
    ('ZZ-SYNTHETIC ALPHA 0042 SANCTIONS-FIXTURE', 'ZZ-SYNTHETIC ALPHA 0042 SANCTIONS-FIXTURE'),
    (null, null)
)

select
    raw,
    expected,
    {{ normalise_name('raw') }} as computed
from cases
where {{ normalise_name('raw') }} is distinct from expected
