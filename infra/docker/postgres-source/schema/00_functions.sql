-- Spec 002 section 4 and design rules 3 to 5.
--
-- Applied by `make schema-apply` as the application role, which owns every object it creates.
-- Every file in this directory is safe to re-apply: see docs/adr/0009-numbered-sql-migrations.md.

create schema if not exists platform;

comment on schema platform is
    'Platform metadata about this source, not the simulated bank''s own data. See spec 002 section 1a.';

-- Maintains updated_at on every table in core, ref and platform. Attached by 30_audit_triggers.sql
-- rather than table by table, so a table added later cannot be missed. Never called by
-- application code: watermark extraction reads updated_at, and a write path that forgets to set
-- it loses rows silently.
create or replace function core.set_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at := now();
    return new;
end;
$$;

-- Double-entry integrity (spec 002 section 4).
--
-- A check constraint cannot express this: it sees one row, and balance is a property of a set of
-- rows that does not exist yet while any single row is being written. The trigger is deferred
-- because a posting batch is assembled across several statements, so an immediate check would
-- reject a batch at its first line, when it is correctly unbalanced.
--
-- The invariant is per currency, not per batch: a EUR debit and a USD credit can sum to zero
-- numerically and mean nothing. A multi-currency posting balances within each currency, with the
-- FX position posted to a conversion account.
create or replace function core.assert_gl_batch_balanced()
returns trigger
language plpgsql
as $$
declare
    v_gl_transaction_id bigint := coalesce(new.gl_transaction_id, old.gl_transaction_id);
    v_currency_code     character(3);
    v_imbalance         numeric(18,4);
begin
    select e.entry_currency_code, sum(e.amount)
      into v_currency_code, v_imbalance
      from core.gl_entries e
     where e.gl_transaction_id = v_gl_transaction_id
     group by e.entry_currency_code
    having sum(e.amount) <> 0
     limit 1;

    if found then
        raise exception
            'gl batch % does not balance in %: debits and credits differ by %',
            v_gl_transaction_id, v_currency_code, v_imbalance
            using errcode = 'integrity_constraint_violation';
    end if;

    return null;
end;
$$;
