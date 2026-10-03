-- Ledger posting batches, the latest state of each, a soft-deleted one absent (specification
-- 008 sections 2 and 4). The ledger posts to its posting date and never moves.
{{ latest_state(ref('br_corebank__gl_transactions'), ['gl_transaction_id']) }}
