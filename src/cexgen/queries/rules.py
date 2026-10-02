"""Step 2's stored rules, broken down with the same analyser as the queries.

    CHECK (age >= 18)                           -> users.age >= 18
    CHECK (lo < hi)                             -> comparison lo < hi
    CHECK (code LIKE 'AC-%' AND length(code) = 6)
                                                -> code LIKE 'AC-%', length(code) = 6
    UNIQUE INDEX ... WHERE deleted_at IS NULL   -> the index only covers rows with deleted_at IS NULL

A rule is `understood` when every part of it became a required predicate or
comparison. Data generation can then satisfy it directly. Anything else is
left to the repair loop (Step 6), with the rule's text as a hint.
"""
from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from ..schema.model import SchemaModel, Table
from ..schema.names import QName
from .expressions import ExpressionAnalyzer
from .model import ColumnRef, Comparison, Predicate


@dataclass(frozen=True)
class Rule:
    table: QName
    name: str
    kind: str                         # check | domain_check | unique_predicate | exclusion_predicate
    expression: str
    predicates: tuple[Predicate, ...] = ()
    comparisons: tuple[Comparison, ...] = ()
    unparsed: tuple[str, ...] = ()

    @property
    def understood(self) -> bool:
        parts = list(self.predicates) + list(self.comparisons)
        return bool(parts) and not self.unparsed and all(p.required for p in parts)


def table_rules(model: SchemaModel) -> dict[QName, tuple[Rule, ...]]:
    out = {}
    for table in model.tables.values():
        rules = [_rule(table, c.name, "domain_check" if c.origin == "domain" else "check", c.expression, "check")
                 for c in table.all_checks]
        rules += [_rule(table, u.name, "unique_predicate", u.predicate, "index_predicate")
                  for u in table.unique_keys if u.predicate]
        rules += [_rule(table, x.name, "exclusion_predicate", x.predicate, "exclusion_predicate")
                  for x in table.exclusions if x.predicate]
        out[table.qname] = tuple(rules)
    return out


def _rule(table: Table, name: str, kind: str, expression: str, clause: str) -> Rule:
    try:
        tree = sqlglot.parse_one(expression, read="postgres")
    except sqlglot.errors.SqlglotError:
        return Rule(table.qname, name, kind, expression, unparsed=(expression,))

    def resolve(col: exp.Column) -> ColumnRef | None:
        ident = col.this
        column = ident.this if getattr(ident, "quoted", False) else col.name.lower()
        return ColumnRef(table.qname, column) if column in table.columns else None

    found = ExpressionAnalyzer(resolve).conditions(tree, clause, "rule")
    return Rule(table.qname, name, kind, expression, tuple(found.predicates), tuple(found.comparisons),
                tuple(found.unparsed))
