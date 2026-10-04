"""What a query's ORDER BY decides, and whether its result is deterministic.

Order is compared only when both queries have a top-level ORDER BY, and only
where ORDER BY decides it: rows with equal sort keys may come in any order.
To know the sort keys, the query is run once more with its ORDER BY
expressions appended to the select list (when they are not already output
columns). That is the "keyed" query; its first columns are the original result.

Not deterministic (a difference there is not trusted):
  LIMIT / OFFSET / FETCH without ORDER BY at the same level   which rows come back is not fixed
  DISTINCT ON without ORDER BY                                 which row of each group is not fixed
"""
from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp

KEY_PREFIX = "_cex_sort_key_"


@dataclass(frozen=True)
class OrderSpec:
    ordered: bool                         # a top-level ORDER BY
    keyed_sql: str | None = None          # the query returning the sort keys too (None: use the query itself)
    key_columns: tuple[int, ...] = ()     # positions of the sort keys in the keyed query's output
    note: str | None = None               # why order cannot be compared, if it cannot


def order_spec(sql: str) -> OrderSpec:
    try:
        tree = sqlglot.parse_one(sql, read="postgres")
    except sqlglot.errors.SqlglotError:
        return OrderSpec(False, note="the query could not be parsed, so its ORDER BY is unknown")
    order = tree.args.get("order")
    if order is None:
        return OrderSpec(False)
    outputs = [_output_name(p) for p in tree.selects]
    projections = [p.unalias().sql("postgres") for p in tree.selects]
    keys, extra = [], []
    for term in order.expressions:
        e = term.this if isinstance(term, exp.Ordered) else term
        index = None
        if isinstance(e, exp.Literal) and not e.is_string and e.this.isdigit():
            index = int(e.this) - 1                                  # ORDER BY 2
        elif isinstance(e, exp.Column) and not e.table and e.name in outputs:
            index = outputs.index(e.name)                            # ORDER BY an output name
        elif e.sql("postgres") in projections:
            index = projections.index(e.sql("postgres"))             # ORDER BY an expression also selected
        if index is not None and 0 <= index < len(outputs):
            keys.append(index)
            continue
        if not isinstance(tree, exp.Select) or any(isinstance(p, exp.Star) for p in tree.selects):
            return OrderSpec(True, note="its ORDER BY uses expressions that cannot be read out, "
                                        "so only the rows are compared")
        extra.append(e)
        keys.append(len(outputs) + len(extra) - 1)
    if not extra:
        return OrderSpec(True, None, tuple(keys))
    keyed = tree.copy()
    for i, e in enumerate(extra):
        keyed = keyed.select(exp.alias_(e.copy(), f"{KEY_PREFIX}{i}"), copy=False)
    return OrderSpec(True, keyed.sql("postgres"), tuple(keys))


def nondeterminism(sql: str) -> list[str]:
    """Reasons the query's result is not fixed by the data alone."""
    try:
        tree = sqlglot.parse_one(sql, read="postgres")
    except sqlglot.errors.SqlglotError:
        return []
    reasons = []
    for node in tree.walk():
        if not isinstance(node, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
            continue
        has_order = node.args.get("order") is not None
        if (node.args.get("limit") or node.args.get("offset") or node.args.get("fetch")) and not has_order:
            reasons.append("LIMIT / OFFSET without ORDER BY: which rows come back is not fixed")
        distinct = node.args.get("distinct") if isinstance(node, exp.Select) else None
        if distinct is not None and distinct.args.get("on") and not has_order:
            reasons.append("DISTINCT ON without ORDER BY: which row of each group comes back is not fixed")
    return sorted(set(reasons))


def _output_name(projection: exp.Expression) -> str:
    return projection.alias_or_name
