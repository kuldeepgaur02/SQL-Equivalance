"""Run Q1 and Q2 on the loaded data.

Both run in ONE transaction, REPEATABLE READ and READ ONLY: they see exactly
the same data, now() / current_date have the same value in both, and nothing
can be written. Each query runs under its own savepoint, so a failing Q1 does
not stop Q2. A query is read up to max_result_rows rows.
"""
from __future__ import annotations

import logging
import time
from collections import Counter

import psycopg2

from .model import QueryRun
from .normalize import row_key
from .ordering import order_spec

log = logging.getLogger(__name__)


def run_pair(conn, q1: str, q2: str, max_rows: int) -> tuple[QueryRun, QueryRun]:
    conn.rollback()
    try:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            return _run(cur, "Q1", q1, max_rows), _run(cur, "Q2", q2, max_rows)
    finally:
        conn.rollback()


def _run(cur, label: str, sql: str, max_rows: int) -> QueryRun:
    spec = order_spec(sql)
    notes = (spec.note,) if spec.note else ()
    t0 = time.perf_counter()
    try:
        cur.execute("SAVEPOINT cex_query")
        cur.execute(sql)
        columns, oids, rows, truncated = _fetch(cur, max_rows)
        keys = None
        if spec.ordered and not spec.note:
            if spec.keyed_sql is None:                       # the sort keys are output columns already
                keys = tuple(tuple(r[i] for i in spec.key_columns) for r in rows)
            else:
                keys, rows2 = _keyed(cur, spec, len(columns), max_rows)
                if rows2 is not None and _same_bag(rows2, rows, oids):
                    rows = rows2                             # same rows, now with their sort keys
                else:
                    keys = None
                    notes += ("its ORDER BY keys could not be read reliably, so only the rows are compared",)
        cur.execute("RELEASE SAVEPOINT cex_query")
    except psycopg2.Error as e:
        cur.execute("ROLLBACK TO SAVEPOINT cex_query")
        message = (e.diag.message_primary or e.pgerror or str(e)).strip()
        log.debug("%s failed at run time: %s %s", label, e.pgcode, message)
        return QueryRun(label, ordered=spec.ordered, error={"sqlstate": e.pgcode, "message": message},
                        duration_ms=(time.perf_counter() - t0) * 1000, notes=notes)
    types = _type_names(cur, oids)
    log.debug("%s returned %d row(s)%s in %.1f ms%s", label, len(rows), " (ordered)" if spec.ordered else "",
              (time.perf_counter() - t0) * 1000, " — TRUNCATED" if truncated else "")
    return QueryRun(label, tuple(columns), types, tuple(oids), tuple(rows), keys, spec.ordered,
                    None, truncated, (time.perf_counter() - t0) * 1000, notes)


def _fetch(cur, max_rows: int):
    columns = [d.name for d in cur.description] if cur.description else []
    oids = [d.type_code for d in cur.description] if cur.description else []
    rows = cur.fetchmany(max_rows + 1) if cur.description else []
    truncated = len(rows) > max_rows
    return columns, oids, [tuple(r) for r in rows[:max_rows]], truncated


def _keyed(cur, spec, width: int, max_rows: int):
    cur.execute("SAVEPOINT cex_keyed")
    try:
        cur.execute(spec.keyed_sql)
        _, _, rows, _ = _fetch(cur, max_rows)
        cur.execute("RELEASE SAVEPOINT cex_keyed")
    except psycopg2.Error:
        cur.execute("ROLLBACK TO SAVEPOINT cex_keyed")
        return None, None
    keys = tuple(tuple(r[i] for i in spec.key_columns) for r in rows)
    return keys, [tuple(r[:width]) for r in rows]


def _same_bag(a, b, oids) -> bool:
    return Counter(row_key(r, tuple(oids)) for r in a) == Counter(row_key(r, tuple(oids)) for r in b)


def _type_names(cur, oids) -> tuple[str, ...]:
    if not oids:
        return ()
    cur.execute("SELECT oid, format_type(oid, NULL) FROM pg_type WHERE oid = ANY(%s)", (list(oids),))
    names = dict(cur.fetchall())
    return tuple(names.get(o, str(o)) for o in oids)
