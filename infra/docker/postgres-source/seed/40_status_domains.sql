-- Status, outcome, role and type vocabularies, and the behavioural attributes downstream
-- models read off them.

insert into ref.account_statuses (code, name, is_open, description, is_active) values
    ('pending', 'Pending activation', false, 'Opened but not yet usable', true),
    ('active',  'Active',             true,  null, true),
    ('dormant', 'Dormant',            true,  'No customer activity for an extended period; still open', true),
    ('frozen',  'Frozen',             true,  'Blocked for investigation; still open and still carries a balance', true),
    ('closed',  'Closed',             false, null, true)
on conflict (code) do update
    set name = excluded.name, is_open = excluded.is_open, description = excluded.description,
        is_active = excluded.is_active
  where (ref.account_statuses.name, ref.account_statuses.is_open,
         ref.account_statuses.description, ref.account_statuses.is_active)
        is distinct from (excluded.name, excluded.is_open, excluded.description,
                          excluded.is_active);

insert into ref.transaction_statuses (code, name, is_posted, description, is_active) values
    ('pending',    'Pending',    false, null, true),
    ('authorised', 'Authorised', false, 'Held against the balance but not yet posted', true),
    ('posted',     'Posted',     true,  null, true),
    ('settled',    'Settled',    true,  null, true),
    ('reversed',   'Reversed',   false, null, true),
    ('declined',   'Declined',   false, null, true)
on conflict (code) do update
    set name = excluded.name, is_posted = excluded.is_posted,
        description = excluded.description, is_active = excluded.is_active
  where (ref.transaction_statuses.name, ref.transaction_statuses.is_posted,
         ref.transaction_statuses.description, ref.transaction_statuses.is_active)
        is distinct from (excluded.name, excluded.is_posted, excluded.description,
                          excluded.is_active);

insert into ref.transaction_types (code, name, is_customer_initiated, direction, description, is_active) values
    ('card_purchase',         'Card purchase',                 true,  'debit',  null, true),
    ('card_refund',           'Card refund',                   true,  'credit', null, true),
    ('atm_withdrawal',        'ATM cash withdrawal',           true,  'debit',  null, true),
    ('agent_withdrawal',      'Agent cash withdrawal',         true,  'debit',  null, true),
    ('agent_deposit',         'Agent cash deposit',            true,  'credit', 'The deposits the structuring rule in Q11 counts', true),
    ('transfer_out',          'Outgoing transfer',             true,  'debit',  null, true),
    ('transfer_in',           'Incoming transfer',             true,  'credit', null, true),
    ('standing_order',        'Standing order execution',      true,  'debit',  null, true),
    ('direct_debit',          'Direct debit collection',       true,  'debit',  'Customer initiated because the customer signed the mandate', true),
    ('interest_accrual',      'Interest accrual',              false, 'credit', null, true),
    ('interest_posting',      'Interest posting',              false, 'credit', null, true),
    ('account_fee',           'Account fee',                   false, 'debit',  'Posts whether the customer touched the account or not', true),
    ('maintenance_fee',       'Maintenance fee',               false, 'debit',  null, true),
    ('card_issue_fee',        'Card issuance fee',             false, 'debit',  null, true),
    ('fx_markup',             'Foreign exchange markup',       false, 'debit',  null, true),
    ('chargeback_adjustment', 'Chargeback adjustment',         false, 'credit', 'Raised by the bank, not by the customer', true),
    ('write_off',             'Write-off',                     false, 'debit',  null, true),
    ('internal_reclass',      'Internal reclassification',     false, 'debit',  null, true)
on conflict (code) do update
    set name = excluded.name, is_customer_initiated = excluded.is_customer_initiated,
        direction = excluded.direction, description = excluded.description,
        is_active = excluded.is_active
  where (ref.transaction_types.name, ref.transaction_types.is_customer_initiated,
         ref.transaction_types.direction, ref.transaction_types.description,
         ref.transaction_types.is_active)
        is distinct from (excluded.name, excluded.is_customer_initiated, excluded.direction,
                          excluded.description, excluded.is_active);

-- is_posted is the payment half of the balance reconciliation, mirroring
-- ref.transaction_statuses.is_posted. It says whether this payment's amount is reflected in
-- core.accounts.current_balance_amount, which is what spec 003 invariant 4 sums.
--
-- 'returned' is posted. A settled payment that is later returned did debit the account, and
-- the return arrives as its own row under the 'scheme_return' payment type in the credit
-- direction. Netting it here instead would make one row mean two movements.
insert into ref.payment_statuses (code, name, is_declined, is_final, is_posted, description, is_active) values
    ('initiated', 'Initiated', false, false, false, 'Instruction accepted, account not yet debited', true),
    ('pending',   'Pending',   false, false, false, 'Awaiting scheme processing', true),
    ('booked',    'Booked',    false, false, true,  'Debited from the account, not yet settled with the scheme', true),
    ('settled',   'Settled',   false, true,  true,  null, true),
    ('rejected',  'Rejected',  true,  true,  false, 'Refused by the scheme or the beneficiary bank', true),
    ('returned',  'Returned',  true,  true,  true,  'Settled and then returned; the return is a separate scheme_return payment', true),
    ('cancelled', 'Cancelled', false, true,  false, 'Withdrawn by the customer before settlement', true)
on conflict (code) do update
    set name = excluded.name, is_declined = excluded.is_declined, is_final = excluded.is_final,
        is_posted = excluded.is_posted,
        description = excluded.description, is_active = excluded.is_active
  where (ref.payment_statuses.name, ref.payment_statuses.is_declined,
         ref.payment_statuses.is_final, ref.payment_statuses.is_posted,
         ref.payment_statuses.description, ref.payment_statuses.is_active)
        is distinct from (excluded.name, excluded.is_declined, excluded.is_final,
                          excluded.is_posted, excluded.description, excluded.is_active);

insert into ref.payment_types (code, name, is_customer_initiated, direction, description, is_active) values
    ('sepa_transfer_out',  'SEPA credit transfer, outgoing', true,  'debit',  null, true),
    ('sepa_transfer_in',   'SEPA credit transfer, incoming', true,  'credit', null, true),
    ('sepa_instant_out',   'SEPA instant, outgoing',         true,  'debit',  null, true),
    ('sepa_instant_in',    'SEPA instant, incoming',         true,  'credit', null, true),
    ('sepa_direct_debit',  'SEPA direct debit collection',   true,  'debit',  null, true),
    ('standing_order',     'Standing order execution',       true,  'debit',  null, true),
    ('swift_transfer_out', 'SWIFT transfer, outgoing',       true,  'debit',  null, true),
    ('swift_transfer_in',  'SWIFT transfer, incoming',       true,  'credit', null, true),
    ('scheme_return',      'Scheme return',                  false, 'credit', 'Raised by the scheme or the bank, not by the customer', true),
    ('scheme_recall',      'Scheme recall',                  false, 'debit',  'Raised by the scheme or the bank, not by the customer', true)
on conflict (code) do update
    set name = excluded.name, is_customer_initiated = excluded.is_customer_initiated,
        direction = excluded.direction, description = excluded.description,
        is_active = excluded.is_active
  where (ref.payment_types.name, ref.payment_types.is_customer_initiated,
         ref.payment_types.direction, ref.payment_types.description, ref.payment_types.is_active)
        is distinct from (excluded.name, excluded.is_customer_initiated, excluded.direction,
                          excluded.description, excluded.is_active);

insert into ref.loan_statuses (code, name, is_open, implies_default, description, is_active) values
    ('current',       'Current',       true,  false, null, true),
    ('arrears',       'In arrears',    true,  false, 'Past due but not yet in default', true),
    ('defaulted',     'Defaulted',     true,  true,  '90 or more days past due, or terminated for non-payment', true),
    ('written_off',   'Written off',   false, true,  null, true),
    ('restructured',  'Restructured',  true,  false, 'Terms renegotiated; the origination vintage does not change', true),
    ('closed',        'Closed',        false, false, 'Repaid in full', true)
on conflict (code) do update
    set name = excluded.name, is_open = excluded.is_open,
        implies_default = excluded.implies_default, description = excluded.description,
        is_active = excluded.is_active
  where (ref.loan_statuses.name, ref.loan_statuses.is_open, ref.loan_statuses.implies_default,
         ref.loan_statuses.description, ref.loan_statuses.is_active)
        is distinct from (excluded.name, excluded.is_open, excluded.implies_default,
                          excluded.description, excluded.is_active);

insert into ref.loan_application_statuses (code, name, is_decided, is_approved, description, is_active) values
    ('submitted', 'Submitted', false, false, null, true),
    ('scoring',   'Scoring',   false, false, null, true),
    ('referred',  'Referred',  false, false, 'With a manual underwriter', true),
    ('approved',  'Approved',  true,  true,  null, true),
    ('rejected',  'Rejected',  true,  false, null, true),
    ('withdrawn', 'Withdrawn', false, false, 'Excluded from the approval rate and reported beside it', true),
    ('expired',   'Expired',   false, false, 'Excluded from the approval rate and reported beside it', true)
on conflict (code) do update
    set name = excluded.name, is_decided = excluded.is_decided, is_approved = excluded.is_approved,
        description = excluded.description, is_active = excluded.is_active
  where (ref.loan_application_statuses.name, ref.loan_application_statuses.is_decided,
         ref.loan_application_statuses.is_approved, ref.loan_application_statuses.description,
         ref.loan_application_statuses.is_active)
        is distinct from (excluded.name, excluded.is_decided, excluded.is_approved,
                          excluded.description, excluded.is_active);

insert into ref.login_outcomes (code, name, is_successful, description, is_active) values
    ('success',         'Success',              true,  null, true),
    ('failed_password', 'Failed, password',     false, null, true),
    ('failed_mfa',      'Failed, second factor', false, null, true),
    ('locked',          'Locked out',           false, null, true),
    ('expired',         'Session expired at login', false, null, true)
on conflict (code) do update
    set name = excluded.name, is_successful = excluded.is_successful,
        description = excluded.description, is_active = excluded.is_active
  where (ref.login_outcomes.name, ref.login_outcomes.is_successful,
         ref.login_outcomes.description, ref.login_outcomes.is_active)
        is distinct from (excluded.name, excluded.is_successful, excluded.description,
                          excluded.is_active);

insert into ref.fraud_dispositions (code, name, is_final, is_confirmed_fraud, description, is_active) values
    ('open',            'Open',            false, false, 'Counted in the pending backlog, not in precision', true),
    ('investigating',   'Investigating',   false, false, 'Counted in the pending backlog, not in precision', true),
    ('escalated',       'Escalated',       false, false, 'Counted in the pending backlog, not in precision', true),
    ('confirmed_fraud', 'Confirmed fraud', true,  true,  'The numerator of alert precision', true),
    ('dismissed',       'Dismissed',       true,  false, 'A false positive', true)
on conflict (code) do update
    set name = excluded.name, is_final = excluded.is_final,
        is_confirmed_fraud = excluded.is_confirmed_fraud, description = excluded.description,
        is_active = excluded.is_active
  where (ref.fraud_dispositions.name, ref.fraud_dispositions.is_final,
         ref.fraud_dispositions.is_confirmed_fraud, ref.fraud_dispositions.description,
         ref.fraud_dispositions.is_active)
        is distinct from (excluded.name, excluded.is_final, excluded.is_confirmed_fraud,
                          excluded.description, excluded.is_active);

insert into ref.holder_roles (code, name, is_primary, carries_ownership, description, is_active) values
    ('primary',              'Primary holder',       true,  true,  null, true),
    ('joint',                'Joint holder',         false, true,  'Shares the balance by ownership weight', true),
    ('authorised_signatory', 'Authorised signatory', false, false, 'Operates the account without owning any part of the balance, so ownership_weight is null', true)
on conflict (code) do update
    set name = excluded.name, is_primary = excluded.is_primary,
        carries_ownership = excluded.carries_ownership, description = excluded.description,
        is_active = excluded.is_active
  where (ref.holder_roles.name, ref.holder_roles.is_primary, ref.holder_roles.carries_ownership,
         ref.holder_roles.description, ref.holder_roles.is_active)
        is distinct from (excluded.name, excluded.is_primary, excluded.carries_ownership,
                          excluded.description, excluded.is_active);
