-- Merchant categories, the product catalogue, channels, schemes and interchange.

insert into ref.mcc_bands (code, name, description, is_active) values
    ('standard',    'Standard',            'The default band; everything not separately priced', true),
    ('supermarket', 'Supermarket',         'Grocery and food retail', true),
    ('fuel',        'Fuel',                'Service stations and automated fuel dispensers', true),
    ('travel',      'Travel',              'Airlines, lodging and ground transport', true),
    ('charity',     'Charity',             'Registered charitable organisations', true),
    ('government',  'Government',          'Government services and public bodies', true),
    ('cash',        'Cash disbursement',   'ATM and over-the-counter cash', true)
on conflict (code) do update
    set name = excluded.name, description = excluded.description, is_active = excluded.is_active
  where (ref.mcc_bands.name, ref.mcc_bands.description, ref.mcc_bands.is_active)
        is distinct from (excluded.name, excluded.description, excluded.is_active);

insert into ref.card_product_classes (code, name, description, is_active) values
    ('consumer_debit',    'Consumer debit',    'Regulated intra-EEA interchange cap of 0.20 per cent', true),
    ('consumer_credit',   'Consumer credit',   'Regulated intra-EEA interchange cap of 0.30 per cent', true),
    ('consumer_prepaid',  'Consumer prepaid',  'Treated as debit under the Interchange Fee Regulation', true),
    ('commercial_debit',  'Commercial debit',  'Outside the regulated caps; rates are negotiated', true),
    ('commercial_credit', 'Commercial credit', 'Outside the regulated caps; rates are negotiated', true)
on conflict (code) do update
    set name = excluded.name, description = excluded.description, is_active = excluded.is_active
  where (ref.card_product_classes.name, ref.card_product_classes.description,
         ref.card_product_classes.is_active)
        is distinct from (excluded.name, excluded.description, excluded.is_active);

insert into ref.mcc_codes (code, name, category, band_code, description, is_active) values
    ('5411', 'Grocery stores and supermarkets',      'Retail',        'supermarket', null, true),
    ('5499', 'Miscellaneous food stores',            'Retail',        'supermarket', null, true),
    ('5541', 'Service stations',                     'Fuel',          'fuel',        null, true),
    ('5542', 'Automated fuel dispensers',            'Fuel',          'fuel',        null, true),
    ('5812', 'Eating places and restaurants',        'Dining',        'standard',    null, true),
    ('5814', 'Fast food restaurants',                'Dining',        'standard',    null, true),
    ('5912', 'Drug stores and pharmacies',           'Health',        'standard',    null, true),
    ('5651', 'Family clothing stores',               'Retail',        'standard',    null, true),
    ('5732', 'Electronics stores',                   'Retail',        'standard',    null, true),
    ('5941', 'Sporting goods stores',                'Retail',        'standard',    null, true),
    ('5999', 'Miscellaneous retail',                 'Retail',        'standard',    null, true),
    ('5967', 'Direct marketing, inbound teleservices', 'Digital',     'standard',    null, true),
    ('4899', 'Cable, satellite and pay television',  'Digital',       'standard',    null, true),
    ('7011', 'Lodging, hotels and resorts',          'Travel',        'travel',      null, true),
    ('4511', 'Airlines and air carriers',            'Travel',        'travel',      null, true),
    ('4111', 'Local and suburban commuter transport', 'Travel',       'travel',      null, true),
    ('4121', 'Taxicabs and limousines',              'Travel',        'travel',      null, true),
    ('7994', 'Video game arcades',                   'Entertainment', 'standard',    null, true),
    ('6011', 'Automated cash disbursement',          'Cash',          'cash',        null, true),
    ('6012', 'Financial institutions, merchandise',  'Financial',     'standard',    null, true),
    ('8398', 'Charitable and social service organisations', 'Charity', 'charity',    null, true),
    ('9399', 'Government services',                  'Government',    'government',  null, true)
on conflict (code) do update
    set name = excluded.name, category = excluded.category, band_code = excluded.band_code,
        description = excluded.description, is_active = excluded.is_active
  where (ref.mcc_codes.name, ref.mcc_codes.category, ref.mcc_codes.band_code,
         ref.mcc_codes.description, ref.mcc_codes.is_active)
        is distinct from (excluded.name, excluded.category, excluded.band_code,
                          excluded.description, excluded.is_active);

insert into ref.account_types (code, name, product_class_code, is_deposit_taking, description, is_active) values
    ('current_personal',  'Personal current account',  'current',         true,  null, true),
    ('current_business',  'Business current account',  'current',         true,  null, true),
    ('savings_instant',   'Instant access savings',    'savings',         true,  null, true),
    ('savings_notice',    'Notice savings account',    'savings',         true,  null, true),
    ('loan_servicing',    'Loan servicing account',    'loan',            false, 'Holds a disbursed loan balance; excluded from the deposit balance metric', true),
    ('card_settlement',   'Card settlement account',   'card_settlement', false, 'Internal account the card network settles against', true),
    ('internal_suspense', 'Internal suspense account', 'internal',        false, 'Holds postings pending reclassification', true),
    ('internal_nostro',   'Internal nostro account',   'internal',        false, 'Correspondent bank balance', true)
on conflict (code) do update
    set name = excluded.name, product_class_code = excluded.product_class_code,
        is_deposit_taking = excluded.is_deposit_taking, description = excluded.description,
        is_active = excluded.is_active
  where (ref.account_types.name, ref.account_types.product_class_code,
         ref.account_types.is_deposit_taking, ref.account_types.description,
         ref.account_types.is_active)
        is distinct from (excluded.name, excluded.product_class_code,
                          excluded.is_deposit_taking, excluded.description, excluded.is_active);

-- Codes are twelve characters or fewer because core.cards.card_product_code references them and
-- every character column on that table is held under thirteen (acceptance criterion 4).
insert into ref.card_products (code, name, product_class_code, network, is_commercial, description, is_active) values
    ('dbt_cons_cl', 'Debit Classic',        'consumer_debit',    'visa',       false, null, true),
    ('dbt_cons_pr', 'Debit Premium',        'consumer_debit',    'mastercard', false, null, true),
    ('crd_cons_cl', 'Credit Classic',       'consumer_credit',   'visa',       false, null, true),
    ('crd_cons_gd', 'Credit Gold',          'consumer_credit',   'mastercard', false, null, true),
    ('ppd_cons',    'Prepaid Card',         'consumer_prepaid',  'mastercard', false, null, true),
    ('dbt_comm',    'Business Debit',       'commercial_debit',  'visa',       true,  null, true),
    ('crd_comm',    'Business Credit',      'commercial_credit', 'mastercard', true,  null, true)
on conflict (code) do update
    set name = excluded.name, product_class_code = excluded.product_class_code,
        network = excluded.network, is_commercial = excluded.is_commercial,
        description = excluded.description, is_active = excluded.is_active
  where (ref.card_products.name, ref.card_products.product_class_code, ref.card_products.network,
         ref.card_products.is_commercial, ref.card_products.description, ref.card_products.is_active)
        is distinct from (excluded.name, excluded.product_class_code, excluded.network,
                          excluded.is_commercial, excluded.description, excluded.is_active);

insert into ref.loan_products (code, name, nominal_annual_rate, term_months, is_secured, description, is_active) values
    ('personal_12',     'Personal loan, 12 months',    0.07900000, 12, false, null, true),
    ('personal_24',     'Personal loan, 24 months',    0.08500000, 24, false, null, true),
    ('personal_60',     'Personal loan, 60 months',    0.09250000, 60, false, null, true),
    ('auto_48',         'Vehicle loan, 48 months',     0.05450000, 48, true,  'Secured on the vehicle', true),
    ('home_improve_36', 'Home improvement, 36 months', 0.06850000, 36, false, null, true),
    ('business_24',     'Business loan, 24 months',    0.10500000, 24, false, null, true)
on conflict (code) do update
    set name = excluded.name, nominal_annual_rate = excluded.nominal_annual_rate,
        term_months = excluded.term_months, is_secured = excluded.is_secured,
        description = excluded.description, is_active = excluded.is_active
  where (ref.loan_products.name, ref.loan_products.nominal_annual_rate,
         ref.loan_products.term_months, ref.loan_products.is_secured,
         ref.loan_products.description, ref.loan_products.is_active)
        is distinct from (excluded.name, excluded.nominal_annual_rate, excluded.term_months,
                          excluded.is_secured, excluded.description, excluded.is_active);

insert into ref.channels (code, name, is_digital, description, is_active) values
    ('mobile_app', 'Mobile application', true,  null, true),
    ('web',        'Web banking',        true,  null, true),
    ('api',        'Open banking API',   true,  null, true),
    ('ecommerce',  'Card not present',   true,  'Online card acceptance', true),
    ('pos',        'Point of sale',      false, 'Card present at a merchant terminal', true),
    ('atm',        'ATM',                false, null, true),
    ('agent',      'Partner agent',      false, 'Post office and retail partner counters', true)
on conflict (code) do update
    set name = excluded.name, is_digital = excluded.is_digital,
        description = excluded.description, is_active = excluded.is_active
  where (ref.channels.name, ref.channels.is_digital, ref.channels.description,
         ref.channels.is_active)
        is distinct from (excluded.name, excluded.is_digital, excluded.description,
                          excluded.is_active);

insert into ref.payment_schemes (code, name, is_sepa, settlement_days, description, is_active) values
    ('sepa_ct',    'SEPA credit transfer',         true,  1, null, true),
    ('sepa_inst',  'SEPA instant credit transfer', true,  0, null, true),
    ('sepa_dd',    'SEPA direct debit',            true,  2, null, true),
    ('target2',    'TARGET2 high value',           false, 0, 'Euro real-time gross settlement; not a SEPA retail scheme', true),
    ('swift',      'SWIFT cross-border',           false, 3, null, true)
on conflict (code) do update
    set name = excluded.name, is_sepa = excluded.is_sepa,
        settlement_days = excluded.settlement_days, description = excluded.description,
        is_active = excluded.is_active
  where (ref.payment_schemes.name, ref.payment_schemes.is_sepa,
         ref.payment_schemes.settlement_days, ref.payment_schemes.description,
         ref.payment_schemes.is_active)
        is distinct from (excluded.name, excluded.is_sepa, excluded.settlement_days,
                          excluded.description, excluded.is_active);

-- Exhaustive over card product class, merchant region and MCC band, written as a cross join so
-- that no combination is silently absent. Absence and a null rate would both have to be handled
-- downstream, and one of them is easy to forget.
--
-- The intra-EEA consumer rates are the regulated caps and are used as given. Everything else is
-- null: commercial and inter-regional rates are commercially negotiated, there is no single
-- public number, and inventing one would make Q4 look precise while being arbitrary. A null is
-- an error condition at M6 and is never treated as zero (spec 002 section 11).
insert into ref.interchange_rates
    (card_product_class_code, merchant_region_code, mcc_band_code, rate, valid_from_date,
     description, is_active)
select pc.code,
       r.code,
       b.code,
       case
           when r.code = 'eea' and pc.code in ('consumer_debit', 'consumer_prepaid') then 0.00200000
           when r.code = 'eea' and pc.code = 'consumer_credit' then 0.00300000
           else null
       end,
       date '2020-01-01',
       case
           when r.code = 'eea' and pc.code like 'consumer%'
               then 'Regulated cap, EU Interchange Fee Regulation 2015/751'
           else 'Undecided: negotiated commercially, resolve before M6 per metric_definitions.md'
       end,
       true
  from ref.card_product_classes pc
 cross join ref.regions r
 cross join ref.mcc_bands b
on conflict (card_product_class_code, merchant_region_code, mcc_band_code, valid_from_date)
do update
    set rate = excluded.rate, description = excluded.description, is_active = excluded.is_active
  where (ref.interchange_rates.rate, ref.interchange_rates.description,
         ref.interchange_rates.is_active)
        is distinct from (excluded.rate, excluded.description, excluded.is_active);
