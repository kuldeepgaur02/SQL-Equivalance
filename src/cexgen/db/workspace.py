"""One sandbox database per schema, shared by every case that uses that schema.

    with SchemaWorkspace(schema_sql) as ws:     # create database, run schema SQL, capture seed state
        for case in cases:
            ws.reset()                           # same starting state for every case
            ...                                  # insert rows, run Q1 / Q2 on ws.conn

reset() empties every table, restores the rows and sequence counters the
schema SQL left behind, and refreshes materialized views so they match the
restored data.
"""
from __future__ import annotations

import hashlib

import psycopg2
from psycopg2 import sql

from ..config import Settings, get_settings
from ..errors import SandboxError
from ..sqltext.lexer import fingerprint
from .inventory import Inventory
from .sandbox import Sandbox
from .seed import SeedState, capture, restore


def schema_fingerprint(schema_sql: str) -> str:
    """Stable id of a schema's text: comments, whitespace and keyword case do not change it."""
    return hashlib.sha256(fingerprint(schema_sql).encode()).hexdigest()[:16]


class SchemaWorkspace:
    def __init__(self, schema_sql: str, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.schema_sql = schema_sql
        self.fingerprint = schema_fingerprint(schema_sql)
        self.cache: dict = {}           # per-schema results steps compute once (e.g. the schema model)
        self.sandbox = Sandbox(self.settings)
        try:
            self.inventory: Inventory = self.sandbox.apply_ddl(schema_sql)
            self.seed: SeedState = capture(self.sandbox.conn, self.inventory)
            self._matviews = _matviews_in_refresh_order(self.sandbox.conn)
        except BaseException:
            self.sandbox.close()
            raise

    @property
    def conn(self):
        return self.sandbox.conn

    @property
    def database(self) -> str:
        return self.sandbox.name

    @property
    def server_version(self) -> int:
        return self.sandbox.conn.server_version

    def reset(self) -> None:
        """Put the database back to exactly the state the schema SQL created."""
        restore(self.conn, self.inventory, self.seed)
        self.refresh_materialized_views()

    def refresh_materialized_views(self) -> None:
        """Materialized views keep old results until refreshed; call after any data change."""
        if not self._matviews:
            return
        try:
            with self.conn.cursor() as cur:
                for schema, name in self._matviews:
                    cur.execute(sql.SQL("REFRESH MATERIALIZED VIEW {}.{}").format(
                        sql.Identifier(schema), sql.Identifier(name)))
            self.conn.commit()
        except psycopg2.Error as e:
            self.conn.rollback()
            raise SandboxError(f"could not refresh materialized views: {(e.pgerror or str(e)).strip()}") from None

    def close(self) -> None:
        self.sandbox.close()

    def __enter__(self) -> "SchemaWorkspace":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _matviews_in_refresh_order(conn) -> list[tuple[str, str]]:
    """Materialized views, each after the materialized views it reads from."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT c.oid, n.nspname, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind = 'm' AND n.nspname NOT IN ('pg_catalog', 'information_schema')
            ORDER BY c.oid
        """)
        views = {oid: (s, n) for oid, s, n in cur.fetchall()}
        if not views:
            conn.rollback()
            return []
        cur.execute("""
            SELECT DISTINCT r.ev_class, d.refobjid
            FROM pg_rewrite r JOIN pg_depend d ON d.objid = r.oid AND d.classid = 'pg_rewrite'::regclass
            WHERE r.ev_class = ANY(%s) AND d.refobjid = ANY(%s) AND d.refobjid <> r.ev_class
        """, (list(views), list(views)))
        reads = {oid: set() for oid in views}
        for view, dep in cur.fetchall():
            reads[view].add(dep)
    conn.rollback()

    ordered, done = [], set()
    while len(ordered) < len(views):
        ready = [v for v in views if v not in done and reads[v] <= done]
        if not ready:                      # cannot happen in Postgres (no cyclic views); refresh the rest as-is
            ready = [v for v in views if v not in done]
        for v in ready:
            ordered.append(views[v])
            done.add(v)
    return ordered
