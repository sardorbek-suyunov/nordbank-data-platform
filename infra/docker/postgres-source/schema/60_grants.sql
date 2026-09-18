-- Spec 002 section 6. Run as the application role, which owns all three schemas, so the
-- default privileges below are its own.
--
-- core and ref already carry usage and default privileges from the M1 init scripts. They are
-- restated here because platform did not exist then, and because a grant that lives only in a
-- script that runs once on an empty volume is invisible to anyone reading the DDL.

do $$
declare
    v_reader text := current_setting('nordbank.read_user', true);
begin
    if v_reader is null or v_reader = '' then
        raise exception
            'nordbank.read_user is not set; schema-apply must pass the extraction role name';
    end if;

    execute format('grant usage on schema core, ref, platform to %I', v_reader);
    execute format('grant select on all tables in schema core, ref, platform to %I', v_reader);

    execute format(
        'alter default privileges in schema core grant select on tables to %I', v_reader);
    execute format(
        'alter default privileges in schema ref grant select on tables to %I', v_reader);
    execute format(
        'alter default privileges in schema platform grant select on tables to %I', v_reader);
end;
$$;
