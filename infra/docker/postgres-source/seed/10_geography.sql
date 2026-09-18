-- Regions, countries and the currencies the bank operates in.
--
-- Every statement in this directory uses `on conflict ... do update ... where` the incoming
-- values are distinct from the stored ones. The `where` clause is what makes acceptance
-- criterion 1 true: a plain `do update` would move updated_at on every row of every re-run, so
-- re-applying would change something and every extraction run would re-read all reference data.

insert into ref.regions (code, name, description, is_active) values
    ('eea',              'European Economic Area',   'EU member states plus Iceland, Liechtenstein and Norway', true),
    ('europe_non_eea',   'Europe outside the EEA',   'European countries outside the EEA, including the United Kingdom and Switzerland', true),
    ('north_america',    'North America',            'United States and Canada', true),
    ('latin_america',    'Latin America',            'Central and South America', true),
    ('asia_pacific',     'Asia Pacific',             'East Asia, South Asia and Oceania', true),
    ('middle_east_africa', 'Middle East and Africa', 'Middle East and the African continent', true)
on conflict (code) do update
    set name = excluded.name, description = excluded.description, is_active = excluded.is_active
  where (ref.regions.name, ref.regions.description, ref.regions.is_active)
        is distinct from (excluded.name, excluded.description, excluded.is_active);

insert into ref.currencies (code, name, minor_unit, description, is_active) values
    ('EUR', 'Euro',              2, 'Base currency of the platform; every EUR equivalent resolves to it', true),
    ('GBP', 'Pound sterling',    2, null, true),
    ('USD', 'United States dollar', 2, null, true),
    ('CHF', 'Swiss franc',       2, null, true),
    ('SEK', 'Swedish krona',     2, null, true),
    ('DKK', 'Danish krone',      2, null, true),
    ('NOK', 'Norwegian krone',   2, null, true),
    ('PLN', 'Polish zloty',      2, null, true),
    ('JPY', 'Japanese yen',      0, 'Zero minor units: exercises the presentation rounding path', true)
on conflict (code) do update
    set name = excluded.name, minor_unit = excluded.minor_unit,
        description = excluded.description, is_active = excluded.is_active
  where (ref.currencies.name, ref.currencies.minor_unit, ref.currencies.description,
         ref.currencies.is_active)
        is distinct from (excluded.name, excluded.minor_unit, excluded.description,
                          excluded.is_active);

insert into ref.countries (code, name, region_code, is_eea, is_sepa, description, is_active) values
    ('DE', 'Germany',        'eea', true,  true,  null, true),
    ('FR', 'France',         'eea', true,  true,  null, true),
    ('NL', 'Netherlands',    'eea', true,  true,  null, true),
    ('ES', 'Spain',          'eea', true,  true,  null, true),
    ('IT', 'Italy',          'eea', true,  true,  null, true),
    ('SE', 'Sweden',         'eea', true,  true,  null, true),
    ('DK', 'Denmark',        'eea', true,  true,  null, true),
    ('PL', 'Poland',         'eea', true,  true,  null, true),
    ('IE', 'Ireland',        'eea', true,  true,  null, true),
    ('FI', 'Finland',        'eea', true,  true,  null, true),
    ('AT', 'Austria',        'eea', true,  true,  null, true),
    ('BE', 'Belgium',        'eea', true,  true,  null, true),
    ('PT', 'Portugal',       'eea', true,  true,  null, true),
    ('EE', 'Estonia',        'eea', true,  true,  null, true),
    ('LT', 'Lithuania',      'eea', true,  true,  null, true),
    ('LV', 'Latvia',         'eea', true,  true,  null, true),
    ('NO', 'Norway',         'eea', true,  true,  'EEA member outside the European Union', true),
    ('GB', 'United Kingdom', 'europe_non_eea',     false, true,  'Left the EEA; remains a SEPA participant', true),
    ('CH', 'Switzerland',    'europe_non_eea',     false, true,  'SEPA participant outside the EEA', true),
    ('TR', 'Turkey',         'europe_non_eea',     false, true,  'SEPA participant outside the EEA', true),
    ('US', 'United States',  'north_america',      false, false, null, true),
    ('CA', 'Canada',         'north_america',      false, false, null, true),
    ('BR', 'Brazil',         'latin_america',      false, false, null, true),
    ('MX', 'Mexico',         'latin_america',      false, false, null, true),
    ('JP', 'Japan',          'asia_pacific',       false, false, null, true),
    ('SG', 'Singapore',      'asia_pacific',       false, false, null, true),
    ('AU', 'Australia',      'asia_pacific',       false, false, null, true),
    ('IN', 'India',          'asia_pacific',       false, false, null, true),
    ('AE', 'United Arab Emirates', 'middle_east_africa', false, false, null, true),
    ('ZA', 'South Africa',   'middle_east_africa', false, false, null, true)
on conflict (code) do update
    set name = excluded.name, region_code = excluded.region_code, is_eea = excluded.is_eea,
        is_sepa = excluded.is_sepa, description = excluded.description,
        is_active = excluded.is_active
  where (ref.countries.name, ref.countries.region_code, ref.countries.is_eea,
         ref.countries.is_sepa, ref.countries.description, ref.countries.is_active)
        is distinct from (excluded.name, excluded.region_code, excluded.is_eea,
                          excluded.is_sepa, excluded.description, excluded.is_active);
