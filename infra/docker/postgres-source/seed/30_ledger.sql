-- The ledger vocabulary: entry sides, account types and the chart of accounts.
--
-- entry_sides.sign_multiplier must agree with the check constraint on core.gl_entries, which
-- requires a debit to be positive and a credit negative. A check constraint cannot read another
-- table, so this seed is the single place the two are made to agree, and an integration test
-- asserts it rather than trusting that they were.

insert into ref.entry_sides (code, name, sign_multiplier, description, is_active) values
    ('D', 'Debit',  1,  'Stored as a positive amount on core.gl_entries', true),
    ('C', 'Credit', -1, 'Stored as a negative amount on core.gl_entries', true)
on conflict (code) do update
    set name = excluded.name, sign_multiplier = excluded.sign_multiplier,
        description = excluded.description, is_active = excluded.is_active
  where (ref.entry_sides.name, ref.entry_sides.sign_multiplier, ref.entry_sides.description,
         ref.entry_sides.is_active)
        is distinct from (excluded.name, excluded.sign_multiplier, excluded.description,
                          excluded.is_active);

insert into ref.gl_account_types (code, name, normal_side_code, description, is_active) values
    ('asset',     'Asset',     'D', null, true),
    ('liability', 'Liability', 'C', null, true),
    ('equity',    'Equity',    'C', null, true),
    ('income',    'Income',    'C', null, true),
    ('expense',   'Expense',   'D', null, true)
on conflict (code) do update
    set name = excluded.name, normal_side_code = excluded.normal_side_code,
        description = excluded.description, is_active = excluded.is_active
  where (ref.gl_account_types.name, ref.gl_account_types.normal_side_code,
         ref.gl_account_types.description, ref.gl_account_types.is_active)
        is distinct from (excluded.name, excluded.normal_side_code, excluded.description,
                          excluded.is_active);

insert into ref.gl_accounts (code, name, gl_account_type_code, description, is_active) values
    ('1000', 'Cash and balances at central banks', 'asset',     null, true),
    ('1100', 'Loans and advances to customers',    'asset',     null, true),
    ('1200', 'Card settlement receivable',         'asset',     'The internal side of card network settlement, reconciled in Q15', true),
    ('1300', 'Accrued interest receivable',        'asset',     null, true),
    ('2000', 'Customer deposits',                  'liability', null, true),
    ('2100', 'Card settlement payable',            'liability', null, true),
    ('2200', 'Accrued interest payable',           'liability', null, true),
    ('3000', 'Retained earnings',                  'equity',    null, true),
    ('4000', 'Interest income',                    'income',    null, true),
    ('4100', 'Fee and commission income',          'income',    null, true),
    ('4200', 'Interchange income',                 'income',    'Where Q4 revenue posts', true),
    ('4300', 'Foreign exchange income',            'income',    null, true),
    ('5000', 'Interest expense',                   'expense',   null, true),
    ('5100', 'Card scheme fees',                   'expense',   null, true),
    ('5200', 'Loan impairment expense',            'expense',   null, true),
    ('9000', 'Suspense',                           'asset',     'Holds postings pending reclassification', true),
    ('9100', 'Foreign exchange conversion',        'asset',     'The account a multi-currency batch balances through, so that each currency sums to zero on its own', true)
on conflict (code) do update
    set name = excluded.name, gl_account_type_code = excluded.gl_account_type_code,
        description = excluded.description, is_active = excluded.is_active
  where (ref.gl_accounts.name, ref.gl_accounts.gl_account_type_code,
         ref.gl_accounts.description, ref.gl_accounts.is_active)
        is distinct from (excluded.name, excluded.gl_account_type_code, excluded.description,
                          excluded.is_active);
