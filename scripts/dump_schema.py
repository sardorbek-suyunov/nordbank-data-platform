"""Emit the live source schema as normalised, sorted text.

Sorted and fully qualified so that two dumps of the same schema are byte-identical whatever
order the catalogue happens to return, which is what makes `make schema-apply` twice provably a
no-op: dump, apply again, dump, diff.

Writes to stdout. `OUT=<path>` in the Makefile captures it. Nothing generated is committed: a
checked-in schema snapshot is a third artifact that rots unless it is itself guarded, and
`docs/data_dictionary.md` is already the committed representation that `make schema-check`
guards.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import source_db_exec as db  # noqa: E402

SCHEMA_FILTER = "('core', 'ref', 'platform')"

COLUMNS_SQL = f"""
select n.nspname || '.' || c.relname,
       a.attname,
       format_type(a.atttypid, a.atttypmod),
       case when a.attnotnull then 'not null' else 'null' end,
       coalesce(
           case when a.attidentity <> '' then 'identity'
                else pg_get_expr(d.adbin, d.adrelid) end,
           '-')
  from pg_attribute a
  join pg_class c        on c.oid = a.attrelid
  join pg_namespace n    on n.oid = c.relnamespace
  left join pg_attrdef d on d.adrelid = a.attrelid and d.adnum = a.attnum
 where n.nspname in {SCHEMA_FILTER}
   and c.relkind = 'r'
   and a.attnum > 0
   and not a.attisdropped
 order by 1, 2
"""

CONSTRAINTS_SQL = f"""
select n.nspname || '.' || c.relname,
       con.conname,
       pg_get_constraintdef(con.oid)
  from pg_constraint con
  join pg_class c     on c.oid = con.conrelid
  join pg_namespace n on n.oid = c.relnamespace
 where n.nspname in {SCHEMA_FILTER}
 order by 1, 2
"""

INDEXES_SQL = f"""
select n.nspname || '.' || t.relname,
       i.relname,
       pg_get_indexdef(x.indexrelid)
  from pg_index x
  join pg_class i     on i.oid = x.indexrelid
  join pg_class t     on t.oid = x.indrelid
  join pg_namespace n on n.oid = t.relnamespace
 where n.nspname in {SCHEMA_FILTER}
 order by 1, 2
"""

TRIGGERS_SQL = f"""
select n.nspname || '.' || c.relname,
       tg.tgname,
       pg_get_triggerdef(tg.oid)
  from pg_trigger tg
  join pg_class c     on c.oid = tg.tgrelid
  join pg_namespace n on n.oid = c.relnamespace
 where n.nspname in {SCHEMA_FILTER}
   and not tg.tgisinternal
 order by 1, 2
"""

FUNCTIONS_SQL = """
select n.nspname || '.' || p.proname,
       md5(pg_get_functiondef(p.oid))
  from pg_proc p
  join pg_namespace n on n.oid = p.pronamespace
 where n.nspname in ('core', 'ref', 'platform')
 order by 1, 2
"""

GRANTS_SQL = f"""
select n.nspname || '.' || c.relname,
       g.grantee,
       string_agg(distinct g.privilege_type, ',' order by g.privilege_type)
  from information_schema.role_table_grants g
  join pg_class c     on c.relname = g.table_name
  join pg_namespace n on n.oid = c.relnamespace and n.nspname = g.table_schema
 where n.nspname in {SCHEMA_FILTER}
 group by 1, 2
 order by 1, 2
"""

SECTIONS = (
    ("columns", COLUMNS_SQL),
    ("constraints", CONSTRAINTS_SQL),
    ("indexes", INDEXES_SQL),
    ("triggers", TRIGGERS_SQL),
    ("functions", FUNCTIONS_SQL),
    ("grants", GRANTS_SQL),
)


def dump() -> str:
    execute = db.executor()
    lines: list[str] = []
    for name, sql in SECTIONS:
        rows = execute(sql)
        lines.append(f"# {name} ({len(rows)})")
        lines.extend("  " + " | ".join(row) for row in sorted(rows))
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    db.require_stack()
    sys.stdout.write(dump())
    return 0


if __name__ == "__main__":
    sys.exit(main())
