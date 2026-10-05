"""What Step 3 learns from a query (and from Step 2's stored rules).

The central idea is the Predicate: "this column (maybe through a JSON path,
a cast or a function) must stand in this relation to these constants", e.g.
orders.amount > 100, lower(users.email) LIKE 'a%', orders.meta->'channel' = 'web'.
Data generation (Step 5) and repair (Step 6) work from predicates.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from ..schema.names import QName
from ..sqltext.names import quote_ident


@dataclass(frozen=True, order=True)
class ColumnRef:
    table: QName
    column: str

    def __str__(self) -> str:
        return f"{self.table}.{quote_ident(self.column)}"


@dataclass(frozen=True)
class Constant:
    value: Any                    # Python value: str, int, Decimal, bool, None, or a list of these
    sql: str                      # as written in the query
    kind: str                     # "string" | "number" | "boolean" | "null" | "array"
    cast: str | None = None       # target type when written with a cast: '2024-01-01'::date, DATE '...'


@dataclass(frozen=True)
class Term:
    """The thing a predicate constrains: a column, possibly seen through a JSON
    path, a function and/or a cast. `(orders.meta->>'k')::int` is
    Term(column=orders.meta, json_path=('k',), cast='int')."""
    column: ColumnRef
    json_path: tuple[str | int, ...] = ()
    function: str | None = None              # lower | upper | length | trim | abs | extract | date_trunc | coalesce
    function_args: tuple[Any, ...] = ()      # extract('year') / date_trunc('month') / coalesce default
    cast: str | None = None
    sql: str = ""

    @property
    def is_plain(self) -> bool:
        return not (self.json_path or self.function or self.cast)


@dataclass(frozen=True)
class Predicate:
    """`term op values`, e.g. amount > 100, status IN ('a','b'), email LIKE 'a%'.

    op is one of: = <> < <= > >= | like ilike similar regex iregex | in between
    | is_null is_true is_false | distinct not_distinct | contains contained
    overlaps has_key has_any_key has_all_keys
    """
    term: Term
    op: str
    values: tuple[Constant, ...]
    negated: bool                 # NOT applied (NOT LIKE, NOT IN, NOT BETWEEN, IS NOT NULL, ...)
    clause: str                   # where | on | having | check | index_predicate | exclusion_predicate
    scope: str                    # main | cte:<name> | derived | subquery | union
    required: bool                # a top-level AND-ed condition of its clause (not inside OR / NOT (...))
    sql: str = ""


@dataclass(frozen=True)
class Comparison:
    """`term op term`: join keys (u.id = o.user_id) or column-to-column rules (lo < hi)."""
    left: Term
    op: str
    right: Term
    clause: str
    scope: str
    required: bool
    sql: str = ""


@dataclass(frozen=True)
class Join:
    kind: str                     # inner | left | right | full | cross | lateral
    target: str                   # schema.table, or the alias of a CTE / subquery
    table: QName | None           # when the target is a real table or view
    alias: str
    condition: str | None
    scope: str


@dataclass(frozen=True)
class ResultColumn:
    name: str
    type: str                     # Postgres type name, e.g. integer, character varying


@dataclass(frozen=True)
class QueryInfo:
    label: str                    # Q1 | Q2
    sql: str
    parsed: bool                  # False: Postgres accepted it but sqlglot could not parse it
    result: tuple[ResultColumn, ...] = ()
    tables: tuple[QName, ...] = ()           # base tables read (views expanded)
    views: tuple[QName, ...] = ()
    columns: tuple[ColumnRef, ...] = ()
    predicates: tuple[Predicate, ...] = ()
    comparisons: tuple[Comparison, ...] = ()
    joins: tuple[Join, ...] = ()
    constants: tuple[Constant, ...] = ()
    patterns: tuple[str, ...] = ()           # LIKE / SIMILAR / regex patterns
    json_paths: Mapping[ColumnRef, tuple[tuple, ...]] = field(default_factory=dict)
    functions: tuple[str, ...] = ()
    aggregates: tuple[str, ...] = ()
    volatile_functions: tuple[str, ...] = ()  # results can change between runs: comparison unreliable
    features: tuple[str, ...] = ()           # distinct, group_by, outer joins, not_in_subquery, ...
    unparsed: tuple[str, ...] = ()           # conditions that could not be broken down (hints for the LLM)
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class QueryPair:
    q1: QueryInfo
    q2: QueryInfo
    notes: tuple[str, ...] = ()              # e.g. "Q1 returns 2 columns, Q2 returns 3"
