"""The state the schema SQL leaves behind: rows it inserted, and sequence counters.

`capture()` runs once, right after the schema SQL. `restore()` puts that exact
state back before each case, so every case starts from the same point.

Rows are copied with COPY in Postgres text format and written back the same
way. Every type round-trips exactly (numeric scale, timestamps, arrays, json,
bytea) and nothing passes through Python value conversion. Generated columns
are left out: Postgres computes them again.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field

import psycopg2
from psycopg2 import sql

from ..errors import SandboxError
from .inventory import Inventory, Relation


@dataclass(frozen=True)
class TableSeed:
    table: Relation
    columns: tuple[str, ...]      # insertable columns, in COPY order
    data: str                     # COPY text-format payload
    rows: int


@dataclass(frozen=True)
class SequenceState:
    schema: str
    name: str
    last_value: int | None        # None: nextval() never called yet
    start_value: int


@dataclass
class SeedState:
    tables: list[TableSeed] = field(default_factory=list)
    sequences: list[SequenceState] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return sum(t.rows for t in self.tables)


def capture(conn, inventory: Inventory) -> SeedState:
    state = SeedState()
    with conn.cursor() as cur:
        for rel in inventory.tables:
            if not inventory.row_counts.get(rel.qualified):
                continue
            cols = _insertable_columns(cur, rel)
            buf = io.StringIO()
            cur.copy_expert(sql.SQL("COPY (SELECT {} FROM {}) TO STDOUT").format(
                sql.SQL(", ").join(map(sql.Identifier, cols)), rel.own_rows_sql()).as_string(conn), buf)
            state.tables.append(TableSeed(rel, tuple(cols), buf.getvalue(), inventory.row_counts[rel.qualified]))

        cur.execute("""
            SELECT schemaname, sequencename, last_value, start_value FROM pg_sequences
            WHERE schemaname NOT IN ('pg_catalog', 'information_schema') ORDER BY 1, 2
        """)
        state.sequences = [SequenceState(*row) for row in cur.fetchall()]
    conn.rollback()
    return state


def restore(conn, inventory: Inventory, state: SeedState) -> None:
    """Empty every table, then put back the seed rows and the sequence counters.

    Runs as one transaction. Triggers and FK checks are switched off for its
    duration (session_replication_role = replica), so restoring the seed has
    no side effects and does not depend on table order. Without superuser
    rights to do that, tables are restored in repeated passes until FKs are
    satisfied.
    """
    tables = inventory.tables
    try:
        with conn.cursor() as cur:
            replica = _try_replica_role(conn, cur)
            if tables:
                cur.execute(sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(
                    sql.SQL(", ").join(sql.SQL("{}.{}").format(sql.Identifier(t.schema), sql.Identifier(t.name))
                                       for t in tables)))
            if replica:
                for seed in state.tables:
                    _copy_in(conn, cur, seed)
            else:
                _copy_in_passes(conn, cur, state.tables)
            for seq in state.sequences:
                target = sql.SQL("{}.{}").format(sql.Identifier(seq.schema), sql.Identifier(seq.name)).as_string(conn)
                if seq.last_value is None:
                    cur.execute("SELECT setval(%s::regclass, %s, false)", (target, seq.start_value))
                else:
                    cur.execute("SELECT setval(%s::regclass, %s, true)", (target, seq.last_value))
        conn.commit()
    except psycopg2.Error as e:
        conn.rollback()
        raise SandboxError(f"could not reset the tables between cases: {(e.pgerror or str(e)).strip()}") from None


def _insertable_columns(cur, rel: Relation) -> list[str]:
    cur.execute("""
        SELECT a.attname FROM pg_attribute a
        WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped AND a.attgenerated = ''
        ORDER BY a.attnum
    """, (sql.SQL("{}.{}").format(sql.Identifier(rel.schema), sql.Identifier(rel.name)).as_string(cur),))
    return [r[0] for r in cur.fetchall()]


def _copy_in(conn, cur, seed: TableSeed) -> None:
    cur.copy_expert(sql.SQL("COPY {}.{} ({}) FROM STDIN").format(
        sql.Identifier(seed.table.schema), sql.Identifier(seed.table.name),
        sql.SQL(", ").join(map(sql.Identifier, seed.columns))).as_string(conn), io.StringIO(seed.data))


def _copy_in_passes(conn, cur, seeds: list[TableSeed]) -> None:
    pending = list(seeds)
    while pending:
        failed, last_error = [], None
        for seed in pending:
            cur.execute("SAVEPOINT seed_copy")
            try:
                _copy_in(conn, cur, seed)
                cur.execute("RELEASE SAVEPOINT seed_copy")
            except psycopg2.errors.ForeignKeyViolation as e:
                cur.execute("ROLLBACK TO SAVEPOINT seed_copy")
                failed.append(seed)
                last_error = e
        if len(failed) == len(pending):
            raise last_error
        pending = failed


def _try_replica_role(conn, cur) -> bool:
    cur.execute("SAVEPOINT replica_role")
    try:
        cur.execute("SET LOCAL session_replication_role = replica")
        cur.execute("RELEASE SAVEPOINT replica_role")
        return True
    except psycopg2.errors.InsufficientPrivilege:
        cur.execute("ROLLBACK TO SAVEPOINT replica_role")
        return False
