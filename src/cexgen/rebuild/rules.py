"""The rule table's rebuild (--no-llm, and when the LLM call fails): write the
queries' filter values into the base rows.

    round 1   Q1's filters        e.g. amount > 100       -> amount = 100.01
    round 2   Q2's filters
    round 3   both, plus filters inside OR (when nothing required constrains that column)

Join conditions (u.id = o.user_id) are made true by copying one side to the
other. A filter on an FK column moves the parent's key with it. The schema's
own rules are then re-checked: if a filter and a CHECK conflict, the CHECK wins
(the insert would fail otherwise) and the conflict is reported.
"""
from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any, Mapping

from ..baseline.rules import set_path
from ..basedata.model import Cell
from ..basedata.solver import NO_VALUE, best_value, solve_row
from ..queries.model import Predicate, QueryPair
from ..queries.rules import Rule
from ..schema.model import Column, SchemaModel
from ..schema.names import QName
from ..schema.types import Family, TypeInfo
from .rows import REBUILD_RULE, Rows, propagate_fk_values

_CAST_FAMILY = {"int": Family.INTEGER, "integer": Family.INTEGER, "bigint": Family.INTEGER, "smallint": Family.INTEGER,
                "int4": Family.INTEGER, "int8": Family.INTEGER, "numeric": Family.NUMERIC, "decimal": Family.NUMERIC,
                "float": Family.FLOAT, "double precision": Family.FLOAT, "real": Family.FLOAT,
                "boolean": Family.BOOLEAN, "bool": Family.BOOLEAN, "date": Family.DATE, "timestamp": Family.TIMESTAMP}


def rule_rebuild(model: SchemaModel, queries: QueryPair, rules: Mapping[QName, tuple[Rule, ...]] | None,
                 plan, rows: Rows, round_no: int) -> tuple[Rows, list[str], list[str]]:
    """Returns (new rows, what changed, notes)."""
    rows = {t: dict(r) for t, r in rows.items()}
    infos = {1: [queries.q1], 2: [queries.q2]}.get(round_no, [queries.q1, queries.q2])
    changed: set[tuple[QName, str]] = set()
    notes: list[str] = []

    for info in infos:                                    # joins first: equal columns
        for cmp in info.comparisons:
            if not (cmp.required and cmp.op == "=" and cmp.left.is_plain and cmp.right.is_plain):
                continue
            a, b = cmp.left.column, cmp.right.column
            if a.table in rows and b.table in rows and a.column in rows[a.table] and b.column in rows[b.table]:
                if rows[b.table][b.column].value != rows[a.table][a.column].value:
                    rows[b.table][b.column] = Cell(rows[a.table][a.column].value, REBUILD_RULE, cmp.sql)
                    changed.add((b.table, b.column))

    preds = [p for info in infos for p in info.predicates if p.required]
    if round_no >= 3:                                     # OR branches: only where nothing required applies
        constrained = {(p.term.column.table, p.term.column.column) for p in preds}
        preds += [p for info in infos for p in info.predicates if not p.required and p.clause != "on"
                  and (p.term.column.table, p.term.column.column) not in constrained]
    groups: dict[tuple[QName, str, tuple], list[Predicate]] = {}
    for p in preds:
        groups.setdefault((p.term.column.table, p.term.column.column, p.term.json_path), []).append(p)

    for (table, column, path), group in groups.items():
        if table not in rows or column not in rows[table]:
            if table in model.views:
                notes.append(f"filter on view column {table}.{column} not applied (views have no rows of their own)")
            continue
        col = model.tables[table].columns[column]
        current = rows[table][column].value
        if path:
            leaf_col = _leaf_column(col, group)
            found, leaf = _get(current, path)
            value = best_value(leaf_col, group, leaf if found else None)
            if value is NO_VALUE:
                notes.append(f"no value satisfies every filter on {table}.{column}->{'->'.join(map(str, path))}")
                continue
            new = set_path(copy.deepcopy(current) if isinstance(current, (dict, list)) else {}, path, _json(value))
        else:
            new = best_value(col, group, current)
            if new is NO_VALUE:
                notes.append(f"no value satisfies every filter on {table}.{column}: "
                             + "; ".join(p.sql for p in group))
                continue
        if new != current:
            rows[table][column] = Cell(new, REBUILD_RULE, "; ".join(dict.fromkeys(p.sql for p in group)))
            changed.add((table, column))

    propagate_fk_values(model, plan, rows, changed)

    for table in {t for t, _ in changed}:                 # the schema's own rules still have to hold
        values = {c: cell.value for c, cell in rows[table].items()}
        problems = solve_row(model.tables[table], values, (rules or {}).get(table, ()))
        for c, v in values.items():
            if v != rows[table][c].value:
                notes.append(f"{table}.{c}: a schema rule overrode the filter value {rows[table][c].value!r} -> {v!r}")
                rows[table][c] = Cell(v, REBUILD_RULE, "schema rule")
        notes += [f"{table}: {p}" for p in problems if "not understood" not in p]

    described = [f"{t}.{c} = {rows[t][c].value!r}" for t, c in sorted(changed) if c in rows.get(t, {})]
    return rows, described, notes


def _leaf_column(col: Column, group: list[Predicate]) -> Column:
    """The type of a JSON leaf as the filter reads it: (meta->>'k')::int compares integers."""
    cast = next((p.term.cast for p in group if p.term.cast), None)
    family = _CAST_FAMILY.get((cast or "").lower(), Family.TEXT)
    limits = {"min_value": -2 ** 63, "max_value": 2 ** 63 - 1} if family == Family.INTEGER else {}
    t = TypeInfo(col.type.name, cast or "text", cast or "text", family, **limits)
    return Column(col.name, col.position, t, True)


def _get(doc: Any, path: tuple) -> tuple[bool, Any]:
    for key in path:
        if isinstance(key, int) and isinstance(doc, list) and 0 <= key < len(doc):
            doc = doc[key]
        elif isinstance(doc, dict) and str(key) in doc:
            doc = doc[str(key)]
        else:
            return False, None
    return True, doc


def _json(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value
