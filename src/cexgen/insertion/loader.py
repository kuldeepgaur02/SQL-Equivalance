"""Step 6 — INSERT into Postgres, with the repair loop.

    for each step of the insert plan (parents first):
        INSERT the row (a savepoint per row; a cycle's rows together, FKs deferred)
        failed?  PK / UNIQUE, FK, NOT NULL -> fix by code
                 CHECK / unknown           -> LLM (rule table with --no-llm, or when the call fails)
                 retry; after 3 repairs, skip the row
    then the planned UPDATEs (FKs inserted as NULL), COMMIT, and read the tables back.

Before every attempt the row's FK columns are copied again from the parent rows
as Postgres actually stored them, so a repaired parent key carries over to its
children. A parent that ended up without a row turns a nullable FK into NULL,
and skips a child whose FK is NOT NULL.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

import psycopg2
from psycopg2 import sql

from ..basedata.model import BaseData
from ..db.workspace import SchemaWorkspace
from ..journal import Journal, describe_error
from ..journal.entry import to_jsonable
from ..llm.oracle import Oracle
from ..memory import SchemaMemory
from ..ordering.model import DEFERRED, NULL, NULL_THEN_UPDATE, SELF, InsertPlan, Step
from ..ordering.planner import root_table
from ..queries.rules import Rule
from ..schema.model import SchemaModel
from ..schema.names import QName
from .classify import BY_CODE, FOREIGN_KEY, NOT_NULL, UNIQUE, classify
from .fix_code import fix_foreign_key, fix_not_null, fix_unique
from .fix_rules import fix_by_rules
from .llm_repair import llm_fix
from .model import CODE, LLM, RULES, RULES_AFTER_LLM, LoadResult, Repair
from .sql import insert_statement, to_db, update_statement

BASE_ROWS = "base_rows"          # schema memory: a row known to insert cleanly, per table (Step 5 reads it)
FAILED_FIXES = "failed_fixes"    # schema memory: values that failed a constraint (shown to the LLM)
SNAPSHOT_LIMIT = 1000


class Loader:
    def __init__(self, ws: SchemaWorkspace, model: SchemaModel, plan: InsertPlan, base: BaseData,
                 rules: Mapping[QName, tuple[Rule, ...]] | None, oracle: Oracle, journal: Journal,
                 memory: SchemaMemory, max_repairs: int = 3):
        self.ws, self.model, self.plan, self.base = ws, model, plan, base
        self.rules = rules or {}
        self.oracle, self.journal, self.memory = oracle, journal, memory
        self.max_repairs = max_repairs
        self.rows: dict[QName, dict[str, Any]] = {}            # as Postgres stored them
        self.locs: dict[QName, tuple[int, str]] = {}           # (tableoid, ctid), for the planned UPDATEs
        self.skipped: dict[QName, str] = {}
        self.repairs: list[Repair] = []
        self.updates: list[str] = []
        self.failed_updates: list[str] = []
        self.warnings: list[str] = []

    # -------------------------------------------------------------------------
    def load(self) -> LoadResult:
        conn = self.ws.conn
        self._make_deferrable()
        conn.rollback()
        self.cur = conn.cursor()
        try:
            for step in self.plan.steps:
                if step.kind == "cycle":
                    self._cycle(step)
                else:
                    self._single(step.tables[0])
            self._apply_updates()
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            self.cur.close()
        self.ws.refresh_materialized_views()          # they must show the loaded data to Q1 / Q2
        return LoadResult(rows=self.rows, skipped=self.skipped, repairs=tuple(self.repairs),
                          updates=tuple(self.updates), failed_updates=tuple(self.failed_updates),
                          snapshot=read_snapshot(conn, self.model), warnings=tuple(self.warnings))

    # -- one table ------------------------------------------------------------------
    def _single(self, name: QName) -> None:
        row = self.base.values(name)
        attempts: list[dict] = []
        for attempt in range(self.max_repairs + 1):
            reason = self._sync_fks(name, row, {})
            if reason:
                return self._skip(name, reason)
            try:
                self.cur.execute("SAVEPOINT cex_row")
                actual = self._execute_insert(name, row)
                self.cur.execute("RELEASE SAVEPOINT cex_row")
            except psycopg2.Error as e:
                self.cur.execute("ROLLBACK TO SAVEPOINT cex_row")
                row = self._after_failure(name, row, e, attempt, attempts)
                if row is None:
                    return
                continue
            return self._accept(name, actual, row, attempt)

    # -- a cycle of tables ----------------------------------------------------------
    def _cycle(self, step: Step) -> None:
        tables = list(step.tables)
        rows = {t: self.base.values(t) for t in tables}
        attempts: dict[QName, list[dict]] = {t: [] for t in tables}
        names = sql.SQL(", ").join(sql.Identifier(fk.table.schema, fk.name) for fk in step.deferred)
        for attempt in range(self.max_repairs + 1):
            for t in tables:
                reason = self._sync_fks(t, rows[t], rows)
                if reason:
                    for other in tables:
                        self._skip(other, f"cycle {', '.join(map(str, tables))}: {reason}")
                    return
            current = None
            try:
                self.cur.execute("SAVEPOINT cex_step")
                if step.deferred:
                    self.cur.execute(sql.SQL("SET CONSTRAINTS {} DEFERRED").format(names))
                actual = {}
                for t in tables:
                    current = t
                    actual[t] = self._execute_insert(t, rows[t])
                current = None
                if step.deferred:                      # deferred FK errors surface here, inside the savepoint
                    self.cur.execute(sql.SQL("SET CONSTRAINTS {} IMMEDIATE").format(names))
                self.cur.execute("RELEASE SAVEPOINT cex_step")
            except psycopg2.Error as e:
                self.cur.execute("ROLLBACK TO SAVEPOINT cex_step")
                failing = current or self._table_of(e, tables)
                fixed = self._after_failure(failing, rows[failing], e, attempt, attempts[failing])
                if fixed is None:
                    for other in tables:
                        if other != failing:
                            self._skip(other, f"cycle partner {failing} could not be inserted")
                    return
                rows[failing] = fixed
                continue
            for t in tables:
                self._accept(t, actual[t], rows[t], attempt)
            return

    # -- failure -> repair ------------------------------------------------------------
    def _after_failure(self, name: QName, row: dict, e: psycopg2.Error, attempt: int,
                       attempts: list[dict]) -> dict | None:
        """Record the failure, then repair the row. Returns the repaired row, or None (row skipped)."""
        err = describe_error(e)
        category = classify(e)
        self.journal.record("insert", f"INSERT {name}", "failed", error=err,
                            detail={"attempt": attempt + 1, "row": to_jsonable(row), "category": category})
        attempts.append({"row": json.dumps(to_jsonable(row), ensure_ascii=False),
                         "result": f"{err.get('sqlstate')}: {err.get('message', '').splitlines()[0]}"})
        if attempt > 0:
            self._remember_failed_fix(name, err, row)
        if attempt == self.max_repairs:
            self._skip(name, f"still failing after {self.max_repairs} repairs: {err.get('message', '').splitlines()[0]}")
            return None
        fixed, method, note = self._repair(name, row, err, category, attempt + 1, attempts)
        changed = {c: (row.get(c), fixed.get(c)) for c in fixed if fixed.get(c) != row.get(c)} if fixed else {}
        self.repairs.append(Repair(name, attempt + 1, category, err.get("sqlstate") or "", err.get("constraint"),
                                   err.get("message", "").splitlines()[0], method, changed, note))
        self.journal.record("repair", f"repair {name} ({category})", "ok" if fixed else "failed",
                            detail={"attempt": attempt + 1, "method": method, "note": note,
                                    "changed": {c: to_jsonable(v) for c, v in changed.items()}})
        if fixed is None:
            self._skip(name, f"no repair found for {category}: {err.get('message', '').splitlines()[0]}")
        return fixed

    def _repair(self, name: QName, row: dict, err: dict, category: str, attempt: int,
                attempts: list[dict]) -> tuple[dict | None, str | None, str]:
        table = self.model.tables[name]
        constraint = err.get("constraint")
        if category in BY_CODE:                                       # the diagram: fix by code
            if category == UNIQUE:
                fixed = fix_unique(self.cur, table, row, constraint, attempt)
            elif category == FOREIGN_KEY:
                fixed = fix_foreign_key(self.cur, self.model, table, row, constraint)
            else:
                fixed = fix_not_null(self.cur, self.model, table, row, err.get("column"))
            if fixed is not None:
                return fixed, CODE, ""
            # code had nothing to offer (e.g. a key made only of FK columns): treat it as unknown
        if self.oracle.mode == "llm":                                 # the diagram: CHECK / unknown -> LLM
            failed_before = self.memory.get(FAILED_FIXES, _failure_key(name, err)) or []
            fixed, note = llm_fix(self.oracle, table, row, err, attempts, failed_before)
            if fixed is not None:
                return fixed, LLM, note
            fixed = fix_by_rules(self.cur, self.model, table, row, category, err, self.rules, attempt)
            return fixed, (RULES_AFTER_LLM if fixed is not None else None), note
        fixed = fix_by_rules(self.cur, self.model, table, row, category, err, self.rules, attempt)
        return fixed, (RULES if fixed is not None else None), ""

    # -- foreign keys ---------------------------------------------------------------------
    def _sync_fks(self, name: QName, row: dict, pending: Mapping[QName, dict]) -> str | None:
        """Copy FK values from the parent rows as stored. Returns a reason if the row must be skipped."""
        table = self.model.tables[name]
        for p in self.plan.fks_of(name):
            fk = p.fk
            if p.strategy == NULL:
                continue
            if p.strategy == NULL_THEN_UPDATE:
                for c in p.null_columns:
                    row[c] = None
                continue
            if p.strategy == SELF:
                for c, rc in zip(fk.columns, fk.ref_columns):
                    row[c] = row[rc]
                continue
            parent = root_table(self.model, fk.ref_table)
            source = pending.get(parent) if p.strategy == DEFERRED else None
            source = source if source is not None else self.rows.get(parent)
            if source is not None:
                for c, rc in zip(fk.columns, fk.ref_columns):
                    row[c] = source[rc]
                continue
            if table.fk_can_be_null(fk):                       # the parent got no row: NULL if allowed
                for c in (fk.columns if fk.match == "full" else
                          [c for c in fk.columns if table.columns[c].nullable]):
                    row[c] = None
                self.warnings.append(f"{name}: {fk.name} left NULL because {parent} got no row")
                continue
            return f"NOT NULL foreign key {fk.name} needs a row in {parent}, which got none " \
                   f"({self.skipped.get(parent, 'not inserted')})"
        return None

    def _apply_updates(self) -> None:
        for u in self.base.updates:
            if u.table not in self.rows:
                continue
            table = self.model.tables[u.table]
            parent = u.table if u.fk.self_reference else root_table(self.model, u.fk.ref_table)
            source = self.rows.get(parent)
            label = f"{u.table}.{u.fk.name}"
            if source is None:
                self.failed_updates.append(f"{label}: {parent} got no row, the FK stays NULL")
                self.journal.record("insert", f"UPDATE {label}", "skipped", detail={"reason": "parent has no row"})
                continue
            values = {c: source[rc] for c, rc in zip(u.fk.columns, u.fk.ref_columns)}
            oid, ctid = self.locs[u.table]
            try:
                self.cur.execute("SAVEPOINT cex_update")
                self.cur.execute(update_statement(table, list(values)),
                                 [to_db(v, table.columns[c]) for c, v in values.items()] + [oid, ctid])
                actual = self._fetch_returning()
                self.cur.execute("RELEASE SAVEPOINT cex_update")
            except psycopg2.Error as e:
                self.cur.execute("ROLLBACK TO SAVEPOINT cex_update")
                self.failed_updates.append(f"{label}: {(e.pgerror or str(e)).strip().splitlines()[0]}")
                self.journal.record("insert", f"UPDATE {label}", "failed", error=e)
                continue
            self.rows[u.table], self.locs[u.table] = actual
            self.updates.append(label)
            self.journal.record("insert", f"UPDATE {label}", "ok", detail={"set": to_jsonable(values)})

    # -- helpers ------------------------------------------------------------------------------
    def _execute_insert(self, name: QName, row: dict) -> tuple[dict, tuple]:
        table = self.model.tables[name]
        cols = [c for c in table.insertable_columns if c.name in row]
        self.cur.execute(insert_statement(table, cols), [to_db(row[c.name], c) for c in cols])
        return self._fetch_returning()

    def _fetch_returning(self) -> tuple[dict, tuple]:
        names = [d.name for d in self.cur.description]
        values = self.cur.fetchone()
        record = dict(zip(names, values))
        loc = (record.pop("tableoid"), record.pop("ctid"))
        return record, loc

    def _accept(self, name: QName, actual: tuple[dict, tuple], sent: dict, attempt: int) -> None:
        record, loc = actual
        self.rows[name], self.locs[name] = record, loc
        self.journal.record("insert", f"INSERT {name}", "ok",
                            detail={"attempt": attempt + 1, "row": to_jsonable(record)})
        self.memory.put(BASE_ROWS, str(name), to_jsonable(sent))

    def _skip(self, name: QName, reason: str) -> None:
        self.skipped[name] = reason
        self.journal.record("insert", f"skip {name}", "skipped", detail={"reason": reason})

    def _remember_failed_fix(self, name: QName, err: dict, row: dict) -> None:
        key = _failure_key(name, err)
        seen = self.memory.get(FAILED_FIXES, key) or []
        value = to_jsonable(row)
        if value not in seen:
            self.memory.put(FAILED_FIXES, key, (seen + [value])[-10:])

    def _table_of(self, e: psycopg2.Error, tables: list[QName]) -> QName:
        diag = e.diag
        q = QName(diag.schema_name, diag.table_name) if diag.table_name else None
        if q in tables:
            return q
        fk = next((f for t in tables for f in self.model.tables[t].foreign_keys if f.name == diag.constraint_name), None)
        return fk.table if fk is not None and fk.table in tables else tables[0]

    def _make_deferrable(self) -> None:
        done = self.ws.cache.setdefault("deferrable", set())
        todo = [fk for fk in self.plan.make_deferrable if (fk.table, fk.name) not in done]
        if not todo:
            return
        conn = self.ws.conn
        conn.rollback()
        with conn.cursor() as cur:
            for fk in todo:
                try:
                    cur.execute("SAVEPOINT cex_alter")
                    cur.execute(sql.SQL("ALTER TABLE {}.{} ALTER CONSTRAINT {} DEFERRABLE INITIALLY IMMEDIATE").format(
                        sql.Identifier(fk.table.schema), sql.Identifier(fk.table.name), sql.Identifier(fk.name)))
                    cur.execute("RELEASE SAVEPOINT cex_alter")
                    done.add((fk.table, fk.name))
                except psycopg2.Error as e:
                    cur.execute("ROLLBACK TO SAVEPOINT cex_alter")
                    self.warnings.append(f"could not make {fk.table}.{fk.name} deferrable: "
                                         f"{(e.pgerror or str(e)).strip().splitlines()[0]}")
        conn.commit()
        self.journal.record("insert", "make cycle FKs deferrable in the sandbox", "ok",
                            detail={"fks": [f"{fk.table}.{fk.name}" for fk in todo]})


def read_snapshot(conn, model: SchemaModel) -> dict[QName, tuple[dict, ...]]:
    """Every row now in the database, per table: seed rows, base rows, and anything triggers added."""
    out: dict[QName, tuple[dict, ...]] = {}
    conn.rollback()
    try:
        with conn.cursor() as cur:
            for table in model.tables.values():
                if table.kind not in ("table", "partitioned table"):
                    continue                   # partitions are read through their parent
                only = sql.SQL("") if table.kind == "partitioned table" else sql.SQL("ONLY ")
                cur.execute(sql.SQL("SELECT * FROM {}{}.{} ORDER BY ctid LIMIT %s").format(
                    only, sql.Identifier(table.qname.schema), sql.Identifier(table.qname.name)), (SNAPSHOT_LIMIT,))
                rows = cur.fetchall()
                if rows:
                    names = [d.name for d in cur.description]
                    out[table.qname] = tuple(dict(zip(names, r)) for r in rows)
    finally:
        conn.rollback()
    return out


def _failure_key(name: QName, err: dict) -> str:
    return f"{name}|{err.get('constraint') or err.get('sqlstate')}"

