"""A script that recreates the handed-off database, and the proof that it does.

    schema SQL (as given)                          creates the tables (and any seed rows)
    TRUNCATE every table                           so only the handed-off rows remain
    SET session_replication_role = replica         FKs and triggers off while the rows go back in
    INSERT ...                                     every row the database held (seed, base, trigger-added)
    RESET session_replication_role

Values are rendered by psycopg2, the same way the INSERTs of Step 6 sent them.
verify() runs the script in a fresh sandbox database and compares Q1 and Q2
again: the hand-off is only trusted if it gives the same outcome.
"""
from __future__ import annotations

from collections import Counter

import psycopg2
from psycopg2 import sql

from ..compare.compare import compare
from ..compare.execute import run_pair
from ..compare.model import Comparison
from ..compare.normalize import row_key
from ..config import Settings
from ..db.sandbox import Sandbox
from ..errors import CexError
from ..insertion.sql import to_db
from ..schema.model import SchemaModel
from ..schema.names import QName


def data_sql(conn, model: SchemaModel, snapshot: dict[QName, tuple[dict, ...]],
             order: list[QName] | None = None) -> str:
    """`order`: tables to write first (the insert plan's parents-first order), for a readable script."""
    first = [t for t in (order or []) if t in snapshot]
    snapshot = {**{t: snapshot[t] for t in first}, **{t: r for t, r in snapshot.items() if t not in first}}
    tables = [t for t in model.tables.values() if t.kind in ("table", "partitioned table")]
    lines = ["BEGIN;"]
    if tables:
        lines.append(sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE;").format(
            sql.SQL(", ").join(sql.Identifier(t.qname.schema, t.qname.name) for t in tables)).as_string(conn))
    lines.append("SET LOCAL session_replication_role = replica;")
    with conn.cursor() as cur:
        for qname, rows in snapshot.items():
            table = model.tables.get(qname)
            if table is None:
                continue
            cols = [c for c in table.insertable_columns if rows and c.name in rows[0]]
            if not cols:
                lines += [sql.SQL("INSERT INTO {} DEFAULT VALUES;").format(
                    sql.Identifier(qname.schema, qname.name)).as_string(conn)] * len(rows)
                continue
            overriding = "OVERRIDING SYSTEM VALUE " if any(c.identity == "always" for c in cols) else ""
            head = sql.SQL("INSERT INTO {} ({}) " + overriding + "VALUES ").format(
                sql.Identifier(qname.schema, qname.name),
                sql.SQL(", ").join(sql.Identifier(c.name) for c in cols)).as_string(conn)
            for row in rows:
                values = cur.mogrify("(" + ", ".join(["%s"] * len(cols)) + ")",
                                     [to_db(row[c.name], c) for c in cols]).decode()
                lines.append(head + values + ";")
    lines.append("COMMIT;")
    return "\n".join(lines) + "\n"


def verify(settings: Settings, schema_sql: str, data: str, q1: str, q2: str,
           expected: Comparison) -> tuple[bool | None, str]:
    """(True, ...) reproduced; (False, why) not reproduced; (None, why) could not be checked."""
    try:
        with Sandbox(settings) as box:
            box.apply_ddl(schema_sql)
            try:
                with box.conn.cursor() as cur:
                    cur.execute(data)
                box.conn.commit()
            except psycopg2.Error as e:
                box.conn.rollback()
                return None, f"the replay script did not run: {(e.pgerror or str(e)).strip().splitlines()[0]}"
            r1, r2 = run_pair(box.conn, q1, q2, settings.max_result_rows)
            again = compare(r1, r2)
    except CexError as e:
        return None, f"could not verify: {str(e).splitlines()[0]}"
    if (again.outcome, again.kind) != (expected.outcome, expected.kind):
        return False, (f"replayed data gives {again.outcome}{f' ({again.kind})' if again.kind else ''}, "
                       f"expected {expected.outcome}{f' ({expected.kind})' if expected.kind else ''}")
    for label, now, before in (("Q1", r1, expected.q1), ("Q2", r2, expected.q2)):
        if _bag(now) != _bag(before):
            return False, f"replayed data gives {label} a different result than the original database"
    return True, "reproduced in a fresh database: the same Q1 and Q2 results"


def _bag(run) -> Counter | tuple:
    if run.error:
        return ("error", run.error.get("sqlstate"))
    return Counter(row_key(r, run.type_oids) for r in run.rows)
