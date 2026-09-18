-- Spec 002 sections 2 and 4: the double-entry ledger and the constraint that makes it one.

create table if not exists core.gl_transactions (
    gl_transaction_id        bigint generated always as identity primary key,
    gl_transaction_reference varchar(40)  not null unique,
    posting_date             date         not null,
    description              varchar(200),
    -- The business event that produced this batch. It sits on the header rather than on the
    -- line because an event produces a batch and the entries are that batch's lines, and it is
    -- what spec 003 invariant 6 joins on to assert that every monetary event reaches the
    -- ledger. Deliberately polymorphic: source_entity_id points into whichever core table
    -- source_entity_code names, so it cannot carry a foreign key, and referential integrity at
    -- that join is replaced by invariant 6 and a dq test at M7. Four nullable foreign keys
    -- would read as stricter and would not survive the next posting type.
    source_entity_code       varchar(40)  references ref.gl_source_entities (code),
    source_entity_id         bigint,
    created_at               timestamptz  not null default now(),
    updated_at               timestamptz  not null default now(),
    is_deleted               boolean      not null default false,
    -- Referenced by the composite foreign key on gl_entries below, which is what stops an entry
    -- carrying a posting date its own batch does not have.
    constraint gl_transactions_posting_date_uq unique (gl_transaction_id, posting_date),
    -- Half a reference is worse than none: it would satisfy a join on the code and resolve to
    -- nothing, or carry an id nobody can interpret.
    constraint gl_transactions_source_ck
        check (num_nonnulls(source_entity_code, source_entity_id) in (0, 2))
);

-- Re-appliable against a database created before the M2 data half (ADR 0009).
alter table core.gl_transactions
    add column if not exists source_entity_code varchar(40)
        references ref.gl_source_entities (code);
alter table core.gl_transactions
    add column if not exists source_entity_id bigint;

do $$
begin
    if not exists (
        select 1 from pg_constraint
         where conname = 'gl_transactions_source_ck'
           and conrelid = 'core.gl_transactions'::regclass
    ) then
        alter table core.gl_transactions
            add constraint gl_transactions_source_ck
            check (num_nonnulls(source_entity_code, source_entity_id) in (0, 2));
    end if;
end;
$$;

-- Spec 003 invariant 6 joins the ledger back to the event on both columns, so the index is
-- composite. source_entity_code is its leading column, so it also satisfies design rule 12 for
-- that foreign key and the catalogue loop in 50_audit_and_indexes.sql leaves it alone rather
-- than adding a second, narrower index over the same data.
create index if not exists ix_gl_transactions_source
    on core.gl_transactions (source_entity_code, source_entity_id);

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
