"""Postgres is the authority on a query: it must run against the schema, and it
must only read.

In a READ ONLY transaction:
  1. EXPLAIN <query>                              valid SQL for this schema? (nothing executes)
  2. SELECT * FROM (<query>) AS _q LIMIT 0        a pure query? (INSERT/UPDATE/DELETE, data-modifying
                                                  WITH and SELECT INTO cannot be wrapped) -> result columns
  3. pg_proc                                      which called functions are volatile (random(), clock_timestamp(),
                                                  nextval() ...): results that change from run to run
"""
from __future__ import annotations

import psycopg2

from ..errors import QueryError
from ..sqltext.lexer import excerpt, line_col
from .model import ResultColumn

_HINTS = {
    "42601": "SQL syntax error.",
    "42703": "A column does not exist in the table it is read from (check the alias and the spelling; "
             "unquoted names are lower-cased).",
    "42P01": "A table does not exist in this schema (unquoted names are lower-cased; check the schema).",
    "42883": "No function or operator matches these argument types; an explicit cast may be needed.",
    "42804": "The two sides have incompatible types.",
    "42702": "A column name is ambiguous; qualify it with its table alias.",
    "42803": "A column must appear in GROUP BY or be used in an aggregate.",
    "25006": "The query tries to write; Q1 and Q2 must be read-only queries.",
    "57014": "The query ran past statement_timeout even on empty tables.",
}


def check_with_postgres(conn, label: str, sql: str) -> tuple[ResultColumn, ...]:
    conn.rollback()
    try:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            _run(cur, label, sql, "EXPLAIN ", "")
            try:
                _run(cur, label, sql, "SELECT * FROM (", "\n) AS _cex_q LIMIT 0")
            except QueryError as e:
                if e.sqlstate == "42601":           # EXPLAIN accepted it, wrapping did not: not a pure query
                    raise QueryError(f"{label} must be a read-only query (SELECT / WITH ... SELECT / VALUES); "
                                     f"statements that write (INSERT, UPDATE, DELETE, data-modifying WITH, "
                                     f"SELECT INTO) are not allowed", sqlstate="25006") from None
                raise
            described = [(d.name, d.type_code) for d in cur.description]   # before the cursor is reused
            names = {}
            if described:
                cur.execute("SELECT oid, format_type(oid, NULL) FROM pg_type WHERE oid = ANY(%s)",
                            ([oid for _, oid in described],))
                names = dict(cur.fetchall())
            return tuple(ResultColumn(name, names.get(oid, str(oid))) for name, oid in described)
    finally:
        conn.rollback()


def volatile_functions(conn, names: list[str]) -> tuple[str, ...]:
    """Functions among `names` whose every overload is VOLATILE."""
    if not names:
        return ()
    conn.rollback()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT proname FROM pg_proc WHERE proname = ANY(%s)
                GROUP BY proname HAVING bool_and(provolatile = 'v') ORDER BY proname
            """, (sorted(set(names)),))
            return tuple(r[0] for r in cur.fetchall())
    finally:
        conn.rollback()


def _run(cur, label: str, sql: str, prefix: str, suffix: str) -> None:
    try:
        cur.execute(prefix + sql + suffix)
    except psycopg2.Error as e:
        cur.connection.rollback()
        raise _query_error(e, label, sql, len(prefix)) from None


def _query_error(e: psycopg2.Error, label: str, sql: str, shift: int) -> QueryError:
    diag = e.diag
    message = (diag.message_primary or str(e)).strip()
    if diag.message_detail:
        message += f" ({diag.message_detail.strip()})"
    line = column = snippet = None
    pos = diag.statement_position
    if pos and pos.isdigit():
        offset = int(pos) - 1 - shift
        if 0 <= offset <= len(sql):
            line, column = line_col(sql, offset)
            snippet = excerpt(sql, offset)
    return QueryError(f"{label} is not valid for this schema: {message}", sqlstate=e.pgcode, line=line,
                      column=column, excerpt=snippet, hint=diag.message_hint or _HINTS.get(e.pgcode or ""))
