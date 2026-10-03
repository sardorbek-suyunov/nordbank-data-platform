-- Accounts, one row per version (specification 008 section 2). The balance is excluded from the
-- projection: planning measured 13,441 of 13,469 account changes, 99.8 per cent, moving only the
-- balance, and no question reads an account's attributes at a balance's grain. A balance-only
-- change opens no version and the balance is not carried; every observed balance is in
-- `sl_account_balance_observations` instead.
{{ scd2(ref('br_corebank__accounts'), ['account_id'], excluded=['current_balance_amount']) }}
