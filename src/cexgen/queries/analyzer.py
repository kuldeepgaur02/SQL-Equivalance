"""Step 3 — Parse queries (sqlglot): Q1 / Q2 -> QueryInfo.

Order of authority:
  1. Postgres checks the query (valid for this schema, read-only, result columns).
  2. sqlglot breaks it down: tables, columns, filters, joins, constants, JSON paths, features.
If sqlglot cannot parse a query Postgres accepted, the case continues with
`parsed=False`: its filters are unknown, and the warning says so.
"""
from __future__ import annotations

import re
from collections import defaultdict

import sqlglot
from sqlglot import exp

from ..errors import QueryError
from ..schema.model import SchemaModel
from ..schema.names import QName
from ..sqltext.lexer import CODE, tokenize
from .expressions import JSON_EXTRACT, Conditions, ExpressionAnalyzer, _JSON_FUNCS, constant
from .model import ColumnRef, Join, QueryInfo
from .resolver import ParsedQuery, scope_label
from .validate import check_with_postgres, volatile_functions

_FUNC_NAME = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_NOT_FUNCTIONS = {"cast", "case", "exists", "any", "all", "array", "row", "values", "not", "and", "or", "in",
                  "select", "with", "from", "where", "coalesce_", "interval"}
_WRITES = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Alter, exp.TruncateTable)


def analyze_query(label: str, sql: str, model: SchemaModel, conn=None) -> QueryInfo:
    parsed, parse_error = None, None
    try:
        parsed = ParsedQuery(sql, model)
    except sqlglot.errors.SqlglotError as e:
        parse_error = e
    if parsed is not None:
        _check_read_only(label, parsed.tree)
    result = check_with_postgres(conn, label, sql) if conn is not None else ()

    if parsed is None:
        names = _function_names_lexical(sql)
        volatile = volatile_functions(conn, names) if conn is not None else ()
        first = str(parse_error).strip().splitlines()[0] if parse_error else "unknown error"
        warnings = [f"sqlglot could not parse {label} ({first}); Postgres accepts it, but its filters are unknown"]
        warnings += _volatile_warning(label, volatile)
        return QueryInfo(label, sql, parsed=False, result=result, functions=tuple(names), volatile_functions=volatile,
                         features=("volatile",) if volatile else (), warnings=tuple(warnings))

    info = _Analysis(label, parsed, model).run()
    volatile = volatile_functions(conn, list(info["functions"])) if conn is not None else ()
    warnings = list(info.pop("warnings")) + _volatile_warning(label, volatile)
    features = set(info.pop("features")) | ({"volatile"} if volatile else set())
    return QueryInfo(label, sql, parsed=True, result=result, volatile_functions=volatile,
                     features=tuple(sorted(features)), warnings=tuple(warnings), **info)


def pair_notes(q1: QueryInfo, q2: QueryInfo, identical: bool) -> tuple[str, ...]:
    notes = []
    if identical:
        notes.append("Q1 and Q2 are the same query text")
    if q1.result and q2.result:
        if len(q1.result) != len(q2.result):
            notes.append(f"Q1 returns {len(q1.result)} column(s), Q2 returns {len(q2.result)}: "
                         f"any non-empty result already differs")
        else:
            for i, (a, b) in enumerate(zip(q1.result, q2.result), 1):
                if a.type != b.type:
                    notes.append(f"column {i}: Q1 gives {a.type}, Q2 gives {b.type} (compared by value)")
    if q1.volatile_functions or q2.volatile_functions:
        notes.append("a volatile function makes results run-dependent; differences must be re-checked")
    return tuple(notes)


class _Analysis:
    def __init__(self, label: str, parsed: ParsedQuery, model: SchemaModel):
        self.label, self.parsed, self.model = label, parsed, model
        self.cond = Conditions()
        self.columns: set[ColumnRef] = set()
        self.joins: list[Join] = []
        self.json_paths: dict[ColumnRef, list[tuple]] = defaultdict(list)
        self.features: set[str] = set()

    def run(self) -> dict:
        tree = self.parsed.tree
        for scope in self.parsed.scopes:
            label = scope_label(scope)
            analyzer = ExpressionAnalyzer(lambda c, s=scope: self.parsed.resolve_column(c, s))
            for col in scope.columns:
                ref = self.parsed.resolve_column(col, scope)
                if ref:
                    self.columns.add(ref)
            sel = scope.expression
            if isinstance(sel, exp.Select):
                self._select(sel, label, analyzer)
            for node in scope.find_all(*JSON_EXTRACT, exp.Anonymous):
                if isinstance(node, exp.Anonymous) and str(node.this).lower() not in _JSON_FUNCS:
                    continue
                if isinstance(node.parent, JSON_EXTRACT):
                    continue
                term = analyzer.term(node)
                if term and term.json_path and term.json_path not in self.json_paths[term.column]:
                    self.json_paths[term.column].append(term.json_path)
        self._features(tree)

        tables, views = self._tables(tree)
        constants, seen = [], set()
        for node in tree.find_all(exp.Literal, exp.Boolean, exp.Null, exp.RawString, exp.ByteString):
            c = constant(node)
            if c is not None and c.sql not in seen:
                seen.add(c.sql)
                constants.append(c)
        functions, aggregates = self._functions(tree)
        patterns = []
        for p in self.cond.predicates:
            if p.op in ("like", "ilike", "similar", "regex", "iregex") and p.values[0].value not in patterns:
                patterns.append(p.values[0].value)
        warnings = list(self.parsed.warnings)
        warnings += [f"{self.label}: table {t} is not in the schema" for t in self.parsed.unresolved_tables]
        if "locking" in self.features:
            warnings.append(f"{self.label} uses FOR UPDATE/SHARE; it takes row locks but is otherwise read-only")
        return dict(
            tables=tuple(tables), views=tuple(views), columns=tuple(sorted(self.columns)),
            predicates=tuple(self.cond.predicates), comparisons=tuple(self.cond.comparisons),
            joins=tuple(self.joins), constants=tuple(constants), patterns=tuple(patterns),
            json_paths={k: tuple(v) for k, v in self.json_paths.items() if v},
            functions=tuple(functions), aggregates=tuple(aggregates),
            unparsed=tuple(self.cond.unparsed), features=self.features, warnings=warnings,
        )

    # -- clauses ------------------------------------------------------------------
    def _select(self, sel: exp.Select, label: str, analyzer: ExpressionAnalyzer) -> None:
        where = sel.args.get("where")
        if where is not None:
            analyzer.conditions(where.this, "where", label, True, self.cond)
        for join in sel.args.get("joins") or []:
            kind = _join_kind(join)
            self.features.add(f"{kind}_join")
            on = join.args.get("on")
            # Only an inner join's ON filters the result; an outer join's ON only decides matches.
            analyzer.conditions(on, "on", label, kind in ("inner", "cross"), self.cond)
            target, table, alias = self._join_target(join.this)
            using = join.args.get("using")
            condition = on.sql("postgres") if on is not None else (
                "USING (" + ", ".join(u.sql("postgres") for u in using) + ")" if using else None)
            self.joins.append(Join(kind, target, table, alias, condition, label))
        having = sel.args.get("having")
        if having is not None:
            analyzer.conditions(having.this, "having", label, True, self.cond)

    def _join_target(self, node) -> tuple[str, QName | None, str]:
        alias = node.alias_or_name if hasattr(node, "alias_or_name") else ""
        if isinstance(node, exp.Table):
            q = self.parsed.table_of(node)
            return (str(q) if q else node.sql("postgres")), q, alias
        return alias or node.sql("postgres")[:60], None, alias

    def _tables(self, tree) -> tuple[list[QName], list[QName]]:
        tables, views = [], []
        for t in tree.find_all(exp.Table):
            q = self.parsed.table_of(t)
            if q is None:
                continue
            if q in self.model.views:
                if q not in views:
                    views.append(q)
                for base in self._view_bases(q, set()):
                    if base not in tables:
                        tables.append(base)
            elif q not in tables:
                tables.append(q)
        return tables, views

    def _view_bases(self, view: QName, seen: set) -> list[QName]:
        out = []
        for r in self.model.views[view].reads:
            if r in seen:
                continue
            seen.add(r)
            out += self._view_bases(r, seen) if r in self.model.views else [r]
        return out

    # -- features -----------------------------------------------------------------
    def _features(self, tree) -> None:
        f = self.features
        for node in tree.walk():
            if isinstance(node, exp.Union):
                f.add("union" if node.args.get("distinct") else "union_all")
            elif isinstance(node, exp.Intersect):
                f.add("intersect")
            elif isinstance(node, exp.Except):
                f.add("except")
            elif isinstance(node, exp.Window):
                f.add("window")
            elif isinstance(node, exp.Exists):
                f.add("not_exists" if _negated(node) else "exists")
            elif isinstance(node, exp.In) and node.args.get("query") is not None:
                f.add("not_in_subquery" if _negated(node) or node.args.get("negate") else "in_subquery")
            elif isinstance(node, exp.Subquery):
                f.add("subquery")
            elif isinstance(node, exp.CTE):
                f.add("cte")
            elif isinstance(node, exp.With) and node.args.get("recursive"):
                f.add("recursive_cte")
            elif isinstance(node, exp.Case):
                f.add("case")
            elif isinstance(node, exp.Coalesce):
                f.add("coalesce")
            elif isinstance(node, exp.Nullif):
                f.add("nullif")
            elif isinstance(node, exp.Is) and isinstance(node.expression, exp.Null):
                f.add("null_test")
            elif isinstance(node, (exp.NullSafeEQ, exp.NullSafeNEQ)):
                f.add("is_distinct_from")
            elif isinstance(node, (exp.Like, exp.ILike, exp.SimilarTo)):
                f.add("like")
            elif isinstance(node, (exp.RegexpLike, exp.RegexpILike)):
                f.add("regex")
            elif isinstance(node, exp.Cast):
                f.add("cast")
            elif isinstance(node, JSON_EXTRACT):
                f.add("json")
            elif isinstance(node, (exp.Array, exp.ArrayContainsAll, exp.ArrayOverlaps, exp.Any)):
                f.add("array")
            elif isinstance(node, exp.Values):
                f.add("values")
            elif isinstance(node, exp.Lock):
                f.add("locking")
            elif isinstance(node, exp.Count):
                inner = node.this
                if isinstance(inner, exp.Star):
                    f.add("count_star")
                elif isinstance(inner, exp.Distinct):
                    f.add("count_distinct")
                else:
                    f.add("count_column")
            if isinstance(node, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
                distinct = node.args.get("distinct") if isinstance(node, exp.Select) else None
                if distinct is not None:
                    f.add("distinct_on" if distinct.args.get("on") else "distinct")
                for arg, name in (("group", "group_by"), ("having", "having"), ("order", "order_by"),
                                  ("limit", "limit"), ("offset", "offset")):
                    if node.args.get(arg) is not None:
                        f.add(name)
            if isinstance(node, exp.AggFunc):
                f.add("aggregate")

    def _functions(self, tree) -> tuple[list[str], list[str]]:
        functions, aggregates = [], []
        for node in tree.find_all(exp.Func):
            if isinstance(node, (exp.Connector, exp.Binary)):
                continue
            if isinstance(node, exp.Anonymous):
                name = str(node.this).lower()
            else:
                m = _FUNC_NAME.match(node.sql("postgres"))
                if not m:
                    continue
                name = m.group(1).lower()
            if name in _NOT_FUNCTIONS:
                continue
            if name not in functions:
                functions.append(name)
            if isinstance(node, exp.AggFunc) and name not in aggregates:
                aggregates.append(name)
        return functions, aggregates


def _check_read_only(label: str, tree: exp.Expression) -> None:
    """A clear message for queries that write. (Postgres enforces it too: see validate.py.)"""
    if isinstance(tree, _WRITES) or isinstance(tree, exp.Command):
        word = tree.key.upper() if not isinstance(tree, exp.Command) else str(tree.this).upper()
        raise QueryError(f"{label} must be a read-only query; it is a {word} statement", sqlstate="25006")
    for node in tree.walk():
        if isinstance(node, _WRITES):
            raise QueryError(f"{label} must be a read-only query; it contains a {node.key.upper()} "
                             f"(data-modifying WITH)", sqlstate="25006")
        if isinstance(node, exp.Select) and node.args.get("into") is not None:
            raise QueryError(f"{label} must be a read-only query; SELECT INTO creates a table", sqlstate="25006")


def _negated(node) -> bool:
    parent = node.parent
    while isinstance(parent, exp.Paren):
        parent = parent.parent
    return isinstance(parent, exp.Not)


def _join_kind(join: exp.Join) -> str:
    if isinstance(join.this, exp.Lateral) or join.args.get("kind", "").upper() == "LATERAL":
        return "lateral"
    side = (join.side or "").lower()
    if side in ("left", "right", "full"):
        return side
    if (join.kind or "").upper() == "CROSS" or (join.args.get("on") is None and not join.args.get("using")):
        return "cross"
    return "inner"


def _volatile_warning(label: str, volatile) -> list[str]:
    if not volatile:
        return []
    return [f"{label} calls volatile function(s) {', '.join(volatile)}: its result can change from run to run, "
            f"so a difference between Q1 and Q2 may not be a real counterexample"]


def _function_names_lexical(sql: str) -> list[str]:
    names = []
    for t in tokenize(sql):
        if t.kind == CODE:
            for m in re.finditer(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(", t.text):
                name = m.group(1).lower()
                if name not in _NOT_FUNCTIONS and name not in names:
                    names.append(name)
    return names
