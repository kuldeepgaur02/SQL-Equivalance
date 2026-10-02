"""What the schema SQL actually created, read from the catalog.

This is a census, not the detailed model (columns, keys, CHECKs are Step 2).
It exists to surface situations later steps must know about: rows the schema
inserted itself, triggers and rules that fire on INSERT, views and foreign
tables that cannot hold test data, row-level security, temporary tables.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from psycopg2 import sql

from ..sqltext.names import quote_ident as _ident

# Objects in these schemas, or created by an extension, are not the user's.
_USER_SCHEMA = """
    n.nspname NOT IN ('pg_catalog', 'information_schema')
    AND n.nspname NOT LIKE 'pg\\_toast%'
    AND n.nspname NOT LIKE 'pg\\_temp\\_%'
"""
_NOT_EXTENSION_MEMBER = "NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = {oid} AND d.deptype = 'e')"

_RELKIND = {"r": "table", "p": "partitioned table", "v": "view", "m": "materialized view",
            "f": "foreign table", "S": "sequence"}


@dataclass(frozen=True)
class Relation:
    schema: str
    name: str
    kind: str                      # one of _RELKIND's values
    is_partition: bool = False
    unlogged: bool = False
    row_security: bool = False
    force_row_security: bool = False

    @property
    def qualified(self) -> str:
        return f"{_ident(self.schema)}.{_ident(self.name)}"

    def own_rows_sql(self) -> sql.Composable:
        """FROM-clause target for this relation's own rows. A plain table needs ONLY:
        without it, SELECT also returns rows of tables that INHERIT from it.
        A partitioned table has no rows of its own; reading it reads its partitions."""
        target = sql.SQL("{}.{}").format(sql.Identifier(self.schema), sql.Identifier(self.name))
        return target if self.kind == "partitioned table" else sql.SQL("ONLY ") + target


@dataclass
class Inventory:
    server_version: str
    relations: list[Relation] = field(default_factory=list)
    enums: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    functions: int = 0
    triggers: list[tuple[str, str]] = field(default_factory=list)     # (table, trigger)
    rules: list[tuple[str, str]] = field(default_factory=list)        # (table, rule)
    extensions: list[str] = field(default_factory=list)
    row_counts: dict[str, int] = field(default_factory=dict)          # table -> rows the schema SQL inserted
    temp_relations: int = 0
    notices: list[str] = field(default_factory=list)

    def of_kind(self, *kinds: str) -> list[Relation]:
        return [r for r in self.relations if r.kind in kinds]

    @property
    def tables(self) -> list[Relation]:
        """Tables that can hold rows: plain tables and partitioned parents (not individual partitions)."""
        return [r for r in self.of_kind("table", "partitioned table") if not r.is_partition]

    @property
    def schemas(self) -> list[str]:
        return sorted({r.schema for r in self.relations})

    def warnings(self) -> list[str]:
        out = []
        if not self.tables:
            out.append("the schema creates no tables, so there is nowhere to put test data")
        for table, n in self.row_counts.items():
            if n:
                out.append(f"the schema SQL inserted {n} row(s) into {table}; the input is meant to have no data, "
                           f"so later steps treat these rows as fixed")
        for table, trig in self.triggers:
            out.append(f"trigger {trig} on {table} fires on writes and may change other tables")
        for table, rule in self.rules:
            out.append(f"rule {rule} on {table} rewrites writes to it")
        for r in self.of_kind("foreign table"):
            out.append(f"{r.qualified} is a foreign table and cannot hold test data")
        for r in self.relations:
            if r.force_row_security:
                out.append(f"{r.qualified} forces row-level security, so even its owner sees filtered rows")
        if self.temp_relations:
            out.append(f"the schema SQL created {self.temp_relations} temporary relation(s); "
                       f"they vanish with the session and are ignored")
        out += [f"postgres: {n}" for n in self.notices]
        return out

    def summary(self) -> str:
        lines = [f"server: PostgreSQL {self.server_version}",
                 f"schemas: {', '.join(self.schemas) or '(none)'}"]
        for kind in ("table", "partitioned table", "view", "materialized view", "foreign table", "sequence"):
            rels = [r for r in self.of_kind(kind) if not r.is_partition]
            if rels:
                lines.append(f"{kind}s ({len(rels)}): {', '.join(r.qualified for r in rels)}")
        partitions = [r for r in self.relations if r.is_partition]
        if partitions:
            lines.append(f"partitions ({len(partitions)}): {', '.join(r.qualified for r in partitions)}")
        if self.enums:
            lines.append(f"enum types: {', '.join(self.enums)}")
        if self.domains:
            lines.append(f"domains: {', '.join(self.domains)}")
        if self.functions:
            lines.append(f"functions/procedures: {self.functions}")
        if self.extensions:
            lines.append(f"extensions: {', '.join(self.extensions)}")
        return "\n".join(lines)


def read_inventory(conn) -> Inventory:
    with conn.cursor() as cur:
        cur.execute("SHOW server_version")
        inv = Inventory(server_version=cur.fetchone()[0])

        cur.execute(f"""
            SELECT n.nspname, c.relname, c.relkind, c.relispartition, c.relpersistence = 'u',
                   c.relrowsecurity, c.relforcerowsecurity
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE {_USER_SCHEMA} AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
              AND {_NOT_EXTENSION_MEMBER.format(oid='c.oid')}
            ORDER BY n.nspname, c.relname
        """)
        inv.relations = [Relation(s, n, _RELKIND[k], part, unlogged, rls, force)
                         for s, n, k, part, unlogged, rls, force in cur.fetchall()]

        cur.execute(f"""
            SELECT n.nspname, t.typname, t.typtype
            FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
            WHERE {_USER_SCHEMA} AND t.typtype IN ('e', 'd')
              AND {_NOT_EXTENSION_MEMBER.format(oid='t.oid')}
            ORDER BY 1, 2
        """)
        for s, name, typtype in cur.fetchall():
            (inv.enums if typtype == "e" else inv.domains).append(f"{_ident(s)}.{_ident(name)}")

        cur.execute(f"""
            SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE {_USER_SCHEMA} AND {_NOT_EXTENSION_MEMBER.format(oid='p.oid')}
        """)
        inv.functions = cur.fetchone()[0]

        cur.execute(f"""
            SELECT n.nspname, c.relname, t.tgname
            FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE NOT t.tgisinternal AND {_USER_SCHEMA}
            ORDER BY 1, 2, 3
        """)
        inv.triggers = [(f"{_ident(s)}.{_ident(t)}", g) for s, t, g in cur.fetchall()]

        cur.execute("""
            SELECT schemaname, tablename, rulename FROM pg_rules
            WHERE schemaname NOT IN ('pg_catalog', 'information_schema') ORDER BY 1, 2, 3
        """)
        inv.rules = [(f"{_ident(s)}.{_ident(t)}", r) for s, t, r in cur.fetchall()]

        cur.execute("SELECT extname || ' ' || extversion FROM pg_extension WHERE extname <> 'plpgsql' ORDER BY 1")
        inv.extensions = [r[0] for r in cur.fetchall()]

        cur.execute("""
            SELECT count(*) FROM pg_class
            WHERE relnamespace = pg_my_temp_schema() AND relkind IN ('r', 'p', 'v', 'm', 'S')
        """)
        inv.temp_relations = cur.fetchone()[0]

        # Exact counts are cheap here: the only rows are ones the schema SQL inserted itself.
        for r in inv.tables:
            cur.execute(sql.SQL("SELECT count(*) FROM {}").format(r.own_rows_sql()))
            inv.row_counts[r.qualified] = cur.fetchone()[0]
    conn.rollback()   # the reads above opened a transaction; leave the session idle
    return inv
