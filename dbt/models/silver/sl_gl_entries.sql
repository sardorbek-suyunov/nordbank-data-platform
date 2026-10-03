-- Ledger lines, the latest state of each, a soft-deleted one absent (specification 008 sections
-- 2 and 4). Amounts are signed, debit positive and credit negative, and stay in the entry's
-- currency: the ledger balances per currency and is not converted.
{{ latest_state(ref('br_corebank__gl_entries'), ['gl_entry_id']) }}
