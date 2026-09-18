-- Spec 002 sections 2 and 4: the double-entry ledger and the constraint that makes it one.

create table if not exists core.gl_transactions (
    gl_transaction_id        bigint generated always as identity primary key,
    gl_transaction_reference varchar(40)  not null unique,
    posting_date             date         not null,
    description              varchar(200),
    created_at               timestamptz  not null default now(),
    updated_at               timestamptz  not null default now(),
    is_deleted               boolean      not null default false,
    -- Referenced by the composite foreign key on gl_entries below, which is what stops an entry
    -- carrying a posting date its own batch does not have.
    constraint gl_transactions_posting_date_uq unique (gl_transaction_id, posting_date)
);

create table if not exists core.gl_entries (
    gl_entry_id        bigint generated always as identity primary key,
    gl_transaction_id  bigint       not null references core.gl_transactions (gl_transaction_id),
    -- Held on the entry because spec 002 section 3 requires it here, and made provably equal to
    -- the batch header by the composite foreign key rather than by a convention nobody enforces.
    posting_date       date         not null,
    gl_account_code    varchar(40)  not null references ref.gl_accounts (code),
    entry_side_code    character(1) not null references ref.entry_sides (code),
    -- Signed: debit positive, credit negative, so the balance invariant is literally a sum to
    -- zero. Design rule 10 records the exception to its non-negative guidance, and
    -- docs/conventions.md records it too so that a later reader does not undo it.
    amount             numeric(18,4) not null,
    entry_currency_code character(3) not null references ref.currencies (code),
    -- The customer account the posting relates to, where it relates to one. Interest and fee
    -- postings to internal accounts do not.
    account_id         bigint       references core.accounts (account_id),
    created_at         timestamptz  not null default now(),
    updated_at         timestamptz  not null default now(),
    is_deleted         boolean      not null default false,
    constraint gl_entries_batch_posting_date_fk
        foreign key (gl_transaction_id, posting_date)
        references core.gl_transactions (gl_transaction_id, posting_date),
    -- Binds the sign to the side so the two can never disagree. ref.entry_sides.sign_multiplier
    -- carries the same fact for downstream use; a check constraint cannot read another table,
    -- so the seed is where the two are made to agree and an integration test asserts it.
    constraint gl_entries_sign_matches_side_ck
        check ((entry_side_code = 'D') = (amount > 0)),
    constraint gl_entries_amount_nonzero_ck check (amount <> 0)
);

-- Deferred to commit: a posting batch is assembled across several statements, so an immediate
-- constraint would reject a batch at its first line, when it is correctly unbalanced. Postgres
-- requires a constraint trigger to be FOR EACH ROW, so the queued event count is the row count;
-- the measured cost and the chunked-commit requirement it places on the M3 loader are in
-- docs/adr/0009-numbered-sql-migrations.md.
drop trigger if exists gl_entries_balanced on core.gl_entries;
create constraint trigger gl_entries_balanced
    after insert or update or delete on core.gl_entries
    deferrable initially deferred
    for each row execute function core.assert_gl_batch_balanced();
