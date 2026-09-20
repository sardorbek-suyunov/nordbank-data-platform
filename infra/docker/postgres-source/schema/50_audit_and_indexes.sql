-- Design rules 3, 4, 5 and 12, applied by iterating the catalogue rather than table by table.
--
-- Written as loops on purpose. Sixteen hand-written trigger pairs and forty hand-written
-- foreign key indexes are sixteen and forty chances to forget one on the next table, and the
-- rules they implement are invariants rather than per-table choices. A table added later is
-- covered the next time `make schema-apply` runs, and a table missing its audit columns fails
-- the apply loudly instead of silently extracting nothing.

-- 1. Audit columns. Design rule 3: created_at and updated_at everywhere, is_deleted in core,
--    is_active and no is_deleted in ref, neither in platform.
do $$
declare
    r        record;
    v_faults text[] := array[]::text[];
begin
    for r in
        select n.nspname as schema_name, c.relname as table_name
          from pg_class c
          join pg_namespace n on n.oid = c.relnamespace
         where c.relkind = 'r'
           and n.nspname in ('core', 'ref', 'platform')
         order by 1, 2
    loop
        if not exists (select 1 from information_schema.columns
                        where table_schema = r.schema_name and table_name = r.table_name
                          and column_name = 'created_at') then
            v_faults := v_faults || format('%s.%s is missing created_at', r.schema_name, r.table_name);
        end if;

        if not exists (select 1 from information_schema.columns
                        where table_schema = r.schema_name and table_name = r.table_name
                          and column_name = 'updated_at') then
            v_faults := v_faults || format('%s.%s is missing updated_at', r.schema_name, r.table_name);
        end if;

        if r.schema_name = 'core'
           and not exists (select 1 from information_schema.columns
                            where table_schema = r.schema_name and table_name = r.table_name
                              and column_name = 'is_deleted') then
            v_faults := v_faults || format('core.%s is missing is_deleted', r.table_name);
        end if;

        if r.schema_name = 'ref' then
            if not exists (select 1 from information_schema.columns
                            where table_schema = r.schema_name and table_name = r.table_name
                              and column_name = 'is_active') then
                v_faults := v_faults || format('ref.%s is missing is_active', r.table_name);
            end if;
            if exists (select 1 from information_schema.columns
                        where table_schema = r.schema_name and table_name = r.table_name
                          and column_name = 'is_deleted') then
                v_faults := v_faults || format(
                    'ref.%s carries is_deleted; reference rows are deactivated, never deleted',
                    r.table_name);
            end if;
        end if;
    end loop;

    if array_length(v_faults, 1) > 0 then
        raise exception 'audit column rule violated: %', array_to_string(v_faults, '; ');
    end if;
end;
$$;

-- 2. The updated_at index (design rule 5) and the set_updated_at trigger (design rule 4).
do $$
declare
    r          record;
    v_index    text;
begin
    for r in
        select n.nspname as schema_name, c.relname as table_name
          from pg_class c
          join pg_namespace n on n.oid = c.relnamespace
         where c.relkind = 'r'
           and n.nspname in ('core', 'ref', 'platform')
         order by 1, 2
    loop
        v_index := left(format('ix_%s_updated_at', r.table_name), 63);

        if not exists (select 1 from pg_class i
                         join pg_namespace ns on ns.oid = i.relnamespace
                        where i.relname = v_index and ns.nspname = r.schema_name) then
            execute format('create index %I on %I.%I (updated_at)',
                           v_index, r.schema_name, r.table_name);
        end if;

        execute format('drop trigger if exists set_updated_at on %I.%I',
                       r.schema_name, r.table_name);
        execute format(
            'create trigger set_updated_at before update on %I.%I '
            'for each row execute function core.set_updated_at()',
            r.schema_name, r.table_name);
    end loop;
end;
$$;

-- 3. An index on every foreign key column (design rule 12). Postgres indexes the referenced
--    key, not the referencing column, so without this an unindexed transactions.account_id
--    makes the M3 load and every source-side validation unusable at full scale.
do $$
declare
    r       record;
    v_index text;
    v_cols  text;
begin
    for r in
        select n.nspname as schema_name,
               t.relname as table_name,
               con.conkey as fk_attnums,
               (select array_agg(a.attname order by k.ord)
                  from unnest(con.conkey) with ordinality as k(attnum, ord)
                  join pg_attribute a
                    on a.attrelid = con.conrelid and a.attnum = k.attnum) as fk_columns
          from pg_constraint con
          join pg_class t      on t.oid = con.conrelid
          join pg_namespace n  on n.oid = t.relnamespace
         where con.contype = 'f'
           and n.nspname in ('core', 'ref', 'platform')
         order by 1, 2, con.conname
    loop
        -- An index serves the foreign key when the constraint columns are its leading columns,
        -- in order. A composite index on (a, b) serves a foreign key on (a) but not on (b).
        if exists (
            select 1
              from pg_index i
             where i.indrelid = format('%I.%I', r.schema_name, r.table_name)::regclass
               and (string_to_array(i.indkey::text, ' ')::smallint[])
                   [1:array_length(r.fk_attnums, 1)] = r.fk_attnums
        ) then
            continue;
        end if;

        v_index := left(format('ix_%s_%s', r.table_name, array_to_string(r.fk_columns, '_')), 63);
        v_cols  := (select string_agg(quote_ident(c), ', ') from unnest(r.fk_columns) as c);

        if not exists (select 1 from pg_class i
                         join pg_namespace ns on ns.oid = i.relnamespace
                        where i.relname = v_index and ns.nspname = r.schema_name) then
            execute format('create index %I on %I.%I (%s)',
                           v_index, r.schema_name, r.table_name, v_cols);
        end if;
    end loop;
end;
$$;

-- 4. Access-path indexes: the ones a query needs that a foreign key does not imply.
--
-- The catalogue loop above gives every foreign key column a single-column index, which serves
-- the constraint. It does not serve a query that filters on the key *and* a range of another
-- column, because the second predicate is evaluated after every row for that key has been read.
--
-- `core.login_sessions` is where that bites. Spec 003 invariant 13 and question 19 both ask the
-- same shape of question — this customer's sessions in a window around an instant — and the
-- foreign key index answers it by reading every session that customer ever had. Measured on the
-- `dev` book, 1.5 million sessions over 5,000 customers:
--
--   invariant 13 over the whole book   52.8 s  ->  6.7 s
--   the mutation engine's delta guard   1.9 s  ->  0.02 s
--
-- The composite index is leading on `customer_id`, so it also serves the foreign key and the
-- loop above leaves the single-column one out.
create index if not exists ix_login_sessions_customer_started
    on core.login_sessions (customer_id, started_at);

-- The clearing cycle reads recent business history on every tick: the authorisations that may
-- post today, the payments that may book or settle, the postings that may be reversed, the
-- non-posted rows that may be voided. All four filter on a business timestamp over a window of
-- days, and none of those columns is a foreign key, so the loop above leaves them unindexed and
-- every one of those queries scans the whole table. Measured on the `dev` book's 2.2 million
-- transactions and 470,000 payments, a seven-day window:
--
--   the authorisations that may post today   1,227 ms  ->  25 ms
--   the payments that may book or settle     1,230 ms  ->  16 ms
--
-- `updated_at` is indexed and is not a substitute: it says when the bank last touched a row,
-- and these questions are about when the customer did.
create index if not exists ix_transactions_booked_at on core.transactions (booked_at);
create index if not exists ix_payments_initiated_at on core.payments (initiated_at);
create index if not exists ix_payments_booked_at on core.payments (booked_at);
