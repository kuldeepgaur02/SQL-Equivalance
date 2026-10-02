"""Step 1 — a throwaway database per run, holding the case's empty tables.

Why a whole database and not a schema: real schema files qualify names
(public.users), reset search_path (pg_dump does), create extensions and
schemas of their own. Inside a private database all of that is harmless, and
nothing a run does can touch another run.

Guarantees:
  - the database is created from template0 with UTF8 + C collation, so text
    ordering and comparisons are the same on every machine;
  - the schema SQL runs in one transaction: it applies completely or not at all;
  - session settings the schema SQL changes (search_path, role, ...) are reset
    afterwards;
  - statements with effects outside the database (roles, tablespaces, other
    databases, ALTER SYSTEM) are refused before anything runs;
  - the database is dropped on close, even on errors or Ctrl-C; databases left by
    a killed process are named with their creation time and removed by
    `cleanup_orphans()`.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

import psycopg2
from psycopg2 import sql

from ..config import Settings, get_settings
from ..errors import SandboxError, SchemaError
from ..sqltext.lexer import CODE, excerpt, line_col, split_statements
from .connection import connect
from .diagnostics import schema_error
from .inventory import Inventory, read_inventory

log = logging.getLogger(__name__)

# Statements whose effect lives outside the sandbox database.
_CLUSTER_WIDE = [
    ("create", "role"), ("create", "user"), ("create", "group"),
    ("alter", "role"), ("alter", "user"), ("alter", "group"),
    ("drop", "role"), ("drop", "user"), ("drop", "group"),
    ("create", "database"), ("alter", "database"), ("drop", "database"),
    ("create", "tablespace"), ("alter", "tablespace"), ("drop", "tablespace"),
    ("alter", "system"), ("create", "subscription"),
]


class Sandbox:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.name = _new_name(self.settings.db_prefix)
        self.conn = None
        self._closed = False
        self._admin = connect(self.settings, autocommit=True)
        try:
            self._check_server()
            self._create_database()
            self.conn = connect(self.settings, dbname=self.name)
        except BaseException:
            self._drop()
            self._admin.close()
            raise
        log.debug("sandbox %s created", self.name)

    # -- schema --------------------------------------------------------------
    def apply_ddl(self, ddl: str) -> Inventory:
        """Run the case's schema SQL as one transaction and report what it created."""
        self._ensure_open()
        _refuse_cluster_wide(ddl)
        del self.conn.notices[:]
        try:
            with self.conn.cursor() as cur:
                cur.execute(ddl)          # no parameters, so '%' in the SQL is left alone
            self.conn.commit()
        except psycopg2.Error as e:
            self._rollback()
            raise schema_error(e, ddl) from None
        finally:
            self._reset_session()
        inv = read_inventory(self.conn)
        inv.notices = [n.strip() for n in self.conn.notices if not n.startswith("NOTICE:  drop cascades")]
        return inv

    def inventory(self) -> Inventory:
        self._ensure_open()
        return read_inventory(self.conn)

    # -- lifecycle -------------------------------------------------------------
    def close(self) -> None:
        """Drop the database (unless settings.keep_db) and disconnect. Safe to call twice."""
        if self._closed:
            return
        self._closed = True
        if self.conn is not None and not self.conn.closed:
            self.conn.close()
        if self.settings.keep_db:
            log.warning("keeping sandbox database %s (keep_db)", self.name)
        else:
            self._drop()
        if not self._admin.closed:
            self._admin.close()

    def __enter__(self) -> "Sandbox":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self):  # last resort if a caller forgot close(); never raises
        try:
            if not self._closed and hasattr(self, "_admin"):
                self.close()
        except Exception:
            pass

    # -- internals -------------------------------------------------------------
    def _check_server(self) -> None:
        version = self._admin.server_version
        if version < self.settings.min_server_version:
            raise SandboxError(f"PostgreSQL {_version_text(version)} is too old; "
                               f"{_version_text(self.settings.min_server_version)} or newer is required")

    def _create_database(self) -> None:
        stmt = sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING {} LC_COLLATE {} LC_CTYPE {}").format(
            sql.Identifier(self.name), sql.Literal(self.settings.db_encoding),
            sql.Literal(self.settings.db_locale), sql.Literal(self.settings.db_locale))
        try:
            with self._admin.cursor() as cur:
                cur.execute(stmt)
                cur.execute(sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                    sql.Identifier(self.name), sql.Literal(f"cexgen sandbox, safe to drop")))
        except psycopg2.errors.InsufficientPrivilege:
            raise SandboxError("the Postgres user in CEX_DSN may not create databases (needs CREATEDB)") from None
        except psycopg2.Error as e:
            raise SandboxError(f"could not create sandbox database {self.name}: {(e.pgerror or str(e)).strip()}") from None

    def _drop(self) -> None:
        try:
            with self._admin.cursor() as cur:
                cur.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(self.name)))
        except psycopg2.Error as e:
            log.warning("could not drop sandbox database %s: %s (run `cexgen cleanup` later)",
                        self.name, (e.pgerror or str(e)).strip())

    def _reset_session(self) -> None:
        """Undo session-level changes the schema SQL made (SET search_path, SET ROLE, ...)."""
        try:
            with self.conn.cursor() as cur:
                cur.execute("RESET ALL; RESET ROLE; SET SESSION AUTHORIZATION DEFAULT")
            self.conn.commit()
        except psycopg2.Error as e:
            self._rollback()
            raise SandboxError(f"could not reset the session after the schema SQL: {(e.pgerror or str(e)).strip()}") from None

    def _rollback(self) -> None:
        try:
            self.conn.rollback()
        except psycopg2.InterfaceError as e:   # the connection is gone (e.g. the SQL terminated it)
            raise SandboxError(f"lost the connection to the sandbox database: {e}") from None

    def _ensure_open(self) -> None:
        if self._closed or self.conn is None or self.conn.closed:
            raise SandboxError("the sandbox is closed")


def cleanup_orphans(settings: Settings | None = None, older_than: timedelta = timedelta(hours=1)) -> list[str]:
    """Drop sandbox databases left behind by killed runs. Only names matching
    <prefix>_<14-digit UTC time>_<8 hex> and older than `older_than` are touched."""
    settings = settings or get_settings()
    pattern = re.compile(rf"^{re.escape(settings.db_prefix)}_(\d{{14}})_[0-9a-f]{{8}}$")
    cutoff = datetime.now(timezone.utc) - older_than
    dropped = []
    admin = connect(settings, autocommit=True)
    try:
        with admin.cursor() as cur:
            cur.execute("SELECT datname FROM pg_database WHERE NOT datistemplate")
            for (name,) in cur.fetchall():
                m = pattern.match(name)
                if not m:
                    continue
                created = datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
                if created < cutoff:
                    cur.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
                    dropped.append(name)
    finally:
        admin.close()
    return dropped


def _refuse_cluster_wide(ddl: str) -> None:
    for stmt in split_statements(ddl):
        words = " ".join(t.text for t in stmt.tokens if t.kind == CODE).lower().split()
        if len(words) >= 2 and (words[0], words[1]) in _CLUSTER_WIDE:
            line, col = line_col(ddl, stmt.start)
            raise SchemaError(f"schema SQL contains `{words[0].upper()} {words[1].upper()}`, which changes the "
                              f"whole Postgres server, not just this database",
                              line=line, column=col, excerpt=excerpt(ddl, stmt.start),
                              hint="remove it; roles, databases and tablespaces are not part of a test schema")


def _new_name(prefix: str) -> str:
    stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime())
    return f"{prefix}_{stamp}_{uuid.uuid4().hex[:8]}"     # <= 20 + 1 + 14 + 1 + 8 = 44 chars (limit 63)


def _version_text(num: int) -> str:
    return f"{num // 10000}.{num % 10000}"
