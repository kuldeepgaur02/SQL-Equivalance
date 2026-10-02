"""The rule table for the diagram's "CHECK / unknown -> LLM" branch: used with
--no-llm (the baseline) and when an LLM call fails.

Candidate rows are checked by Postgres itself before an INSERT is retried:
every CHECK of the table (and of its column domains) is evaluated on the
candidate in a SELECT, so "would this pass?" uses Postgres's exact semantics.
"""
from __future__ import annotations

from decimal import Decimal
from itertools import combinations
from typing import Any, Mapping

import psycopg2
from psycopg2 import sql

from ..basedata.defaults import default_value
from ..basedata.solver import place_in_partition, solve_row
from ..basedata.values import fits
from ..queries.rules import Rule
from ..schema.model import Column, SchemaModel, Table
from ..schema.names import QName
from ..schema.types import Family
from .classify import CHECK, DATA, EXCLUSION, PARTITION
from .fix_code import new_value
from .sql import to_db

MAX_EVALUATIONS = 300


def fix_by_rules(cur, model: SchemaModel, table: Table, row: dict, category: str, error: dict,
                 rules: Mapping[QName, tuple[Rule, ...]], attempt: int) -> dict | None:
    if category == PARTITION:
        new = dict(row)
        place_in_partition(model, table, new)
        return new if new != row else None
    if category == CHECK:
        return _fix_check(cur, table, row, error.get("constraint"), rules.get(table.qname, ()))
    if category == DATA:
        return _fix_data(table, row, error)
    if category == EXCLUSION:
        ex = next((x for x in table.exclusions if x.name == error.get("constraint")), None)
        names = [e for e, _ in ex.elements if e in table.columns] if ex else []
        new = dict(row)
        for name in names:
            value = new_value(cur, table, table.columns[name], row.get(name), attempt)
            if value is not None:
                new[name] = value
        return new if new != row else None
    return None                              # trigger errors and the unknown: nothing safe to guess


def checks_pass(cur, table: Table, row: dict) -> bool:
    """Do all of the table's CHECKs (and domain CHECKs) accept this row? Asked of Postgres."""
    checks = table.all_checks
    if not checks:
        return True
    cols = [c for c in table.insertable_columns if c.name in row]
    # The statement takes parameters, so a literal % in a rule (x % 2 = 0, LIKE 'a%') must be written %%.
    condition = sql.SQL(" AND ").join(sql.SQL("(" + c.expression.replace("%", "%%") + ") IS NOT FALSE")
                                      for c in checks)
    stmt = sql.SQL("SELECT {} FROM (SELECT {}) AS _cex_row").format(
        condition, sql.SQL(", ").join(sql.SQL("%s::" + c.type.sql + " AS {}").format(sql.Identifier(c.name))
                                      for c in cols))
    cur.execute("SAVEPOINT cex_check")
    try:
        cur.execute(stmt, [to_db(row[c.name], c) for c in cols])
        ok = bool(cur.fetchone()[0])
        cur.execute("RELEASE SAVEPOINT cex_check")
        return ok
    except psycopg2.Error:
        cur.execute("ROLLBACK TO SAVEPOINT cex_check")
        return False


def _fix_check(cur, table: Table, row: dict, constraint: str | None, rules: tuple[Rule, ...]) -> dict | None:
    failing = next((r for r in rules if r.name == constraint), None)
    if failing is not None and failing.understood:
        new = dict(row)
        solve_row(table, new, (failing,))
        if new != row and checks_pass(cur, table, new):
            return new
    check = next((c for c in table.all_checks if c.name == constraint), None)
    fk_cols = {c for fk in table.foreign_keys for c in fk.columns}
    names = [c for c in (check.columns if check else ()) if c in row and c not in fk_cols] or \
            [c.name for c in table.insertable_columns if c.name in row and c.name not in fk_cols]
    options = {n: [v for v in candidates(table.columns[n], row[n]) if v != row[n]] for n in names}
    budget = MAX_EVALUATIONS
    for n in names:                                     # change one column
        for v in options[n]:
            budget -= 1
            if budget < 0:
                return None
            new = {**row, n: v}
            if checks_pass(cur, table, new):
                return new
    for a, b in combinations(names, 2):                 # then two at once
        for va in options[a][:8]:
            for vb in options[b][:8]:
                budget -= 1
                if budget < 0:
                    return None
                new = {**row, a: va, b: vb}
                if checks_pass(cur, table, new):
                    return new
    return None


def _fix_data(table: Table, row: dict, error: dict) -> dict | None:
    new = dict(row)
    code = error.get("sqlstate") or ""
    if code == "22001":                                 # value too long: cut to the column's length
        for c in table.insertable_columns:
            v = new.get(c.name)
            if isinstance(v, str) and c.type.length is not None and len(v) > c.type.length:
                new[c.name] = v[: c.type.length]
    else:                                               # out of range / bad format: back to the default
        named = error.get("column")
        if named in table.columns:
            targets = [named]
        else:
            numeric = (Family.INTEGER, Family.NUMERIC, Family.FLOAT)
            targets = [c.name for c in table.insertable_columns if c.name in new and (
                not fits(new[c.name], c)                                          # outside its own limits
                or (code == "22003" and c.type.family in numeric and new[c.name] is not None))]
        for name in targets:
            new[name] = default_value(table.columns[name])
    return new if new != row else None


def candidates(column: Column, current: Any) -> list[Any]:
    t = column.type
    f = t.family
    small = list(range(0, 13)) + [-1, 15, 16, 20, 24, 30, 50, 60, 100, 1000]
    if f == Family.INTEGER:
        vals = small + [t.max_value, t.min_value]
    elif f in (Family.NUMERIC, Family.FLOAT):
        vals = [Decimal(v) for v in small] + [Decimal("0.5"), Decimal("-0.5")]
        if f == Family.FLOAT:
            vals = [float(v) for v in vals]
    elif f == Family.TEXT:
        n = t.length or 8
        vals = ["a", "A", "", "aa", "abc", "x1", "1", "a" * n, "Aa"]
        if isinstance(current, str):
            vals += [current.upper(), current.lower(), current.strip()]
    elif f == Family.BOOLEAN:
        vals = [True, False]
    elif f == Family.ENUM:
        vals = list(t.enum_labels)
    elif f == Family.DATE:
        vals = ["2000-01-01", "1970-01-01", "2024-01-01", "2100-01-01"]
    elif f == Family.TIMESTAMP:
        vals = ["2000-01-01 00:00:00", "1970-01-01 00:00:00", "2024-01-01 00:00:00", "2100-01-01 00:00:00"]
    else:
        vals = [default_value(column)]
    if column.nullable:
        vals.append(None)                     # last resort: a CHECK that evaluates to NULL passes
    out = []
    for v in vals:
        if v not in out and (v is None or fits(v, column)):
            out.append(v)
    return out
