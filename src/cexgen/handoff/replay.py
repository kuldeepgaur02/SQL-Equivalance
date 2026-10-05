"""A script that recreates the handed-off database, and the proof that it does.

It needs only the rights of the tables' owner (no superuser), so it runs on any
PostgreSQL where the schema was created:

    BEGIN
    TRUNCATE every table                             only the handed-off rows remain
    DISABLE TRIGGER USER on tables with triggers     rows triggers added are already in the data
    FKs of a cycle -> DEFERRABLE                     only for the script; set back below
    SET CONSTRAINTS ALL DEFERRED
    INSERT ..., parents first (FK graph);            one statement per table, so rows of a table
                                                     may refer to each other
    SET CONSTRAINTS ALL IMMEDIATE                    every FK is checked here
    the cycle FKs back to NOT DEFERRABLE, triggers enabled again
    COMMIT

verify() runs the script in a fresh sandbox database and compares Q1 and Q2
again: the hand-off is only trusted if both results come out exactly the same.
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
from ..ordering.graph import Edge, FkGraph
from ..ordering.planner import root_table
from ..schema.model import SchemaModel
from ..schema.names import QName


def data_sql(conn, model: SchemaModel, snapshot: dict[QName, tuple[dict, ...]],
             header: list[str] | None = None) -> str:
    holding = [t for t in snapshot if t in model.tables]
    edges = [Edge(t, root_table(model, fk.ref_table), fk) for t in holding for fk in model.tables[t].foreign_keys
             if root_table(model, fk.ref_table) in holding and root_table(model, fk.ref_table) != t]
    order = [t for comp in FkGraph(holding, edges).ordered_components() for t in comp]
    cyclic = {t for comp in FkGraph(holding, edges).components() if len(comp) > 1 for t in comp}
    to_defer = [e.fk for e in edges if e.child in cyclic and e.parent in cyclic and not e.fk.deferrable]
    triggered = [t for t in holding if any(trg.enabled for trg in model.tables[t].triggers)]

    def ident(q: QName) -> str:
        return sql.Identifier(q.schema, q.name).as_string(conn)

    lines = [f"-- {h}" for h in (header or [])] + ["BEGIN;"]
    tables = [t for t in model.tables.values() if t.kind in ("table", "partitioned table")]
    if tables:
        lines.append(f"TRUNCATE {', '.join(ident(t.qname) for t in tables)} RESTART IDENTITY CASCADE;")
    lines += [f"ALTER TABLE {ident(t)} DISABLE TRIGGER USER;" for t in triggered]
    lines += [f"ALTER TABLE {ident(fk.table)} ALTER CONSTRAINT {sql.Identifier(fk.name).as_string(conn)} "
              f"DEFERRABLE INITIALLY DEFERRED;" for fk in to_defer]
    lines.append("SET CONSTRAINTS ALL DEFERRED;")
    with conn.cursor() as cur:
        for qname in order:
            rows, table = snapshot[qname], model.tables[qname]
            cols = [c for c in table.insertable_columns if rows and c.name in rows[0]]
            if not cols:
                lines += [f"INSERT INTO {ident(qname)} DEFAULT VALUES;"] * len(rows)
                continue
            overriding = "OVERRIDING SYSTEM VALUE " if any(c.identity == "always" for c in cols) else ""
            values = ",\n    ".join(
                cur.mogrify("(" + ", ".join(["%s"] * len(cols)) + ")", [to_db(r[c.name], c) for c in cols]).decode()
                for r in rows)
            lines.append(f"INSERT INTO {ident(qname)} ({', '.join(sql.Identifier(c.name).as_string(conn) for c in cols)}) "
                         f"{overriding}VALUES\n    {values};")
    lines.append("SET CONSTRAINTS ALL IMMEDIATE;")
    lines += [f"ALTER TABLE {ident(fk.table)} ALTER CONSTRAINT {sql.Identifier(fk.name).as_string(conn)} "
              f"NOT DEFERRABLE;" for fk in to_defer]
    lines += [f"ALTER TABLE {ident(t)} ENABLE TRIGGER USER;" for t in triggered]
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
