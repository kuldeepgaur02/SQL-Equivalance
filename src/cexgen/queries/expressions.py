"""The shared expression analyser: a boolean condition -> predicates.

Used for the WHERE / ON / HAVING of Q1 and Q2, and for Step 2's stored rules
(CHECK constraints, partial-index and exclusion predicates). One analyser, so a
condition means the same thing wherever it appears.

    amount > 100 AND (status = 'paid' OR status = 'new') AND NOT (note LIKE 'x%')

  -> amount > 100            required
     status = 'paid'         not required (inside OR)
     status = 'new'          not required
     note LIKE 'x%' negated  required
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from sqlglot import exp

from .model import ColumnRef, Comparison, Constant, Predicate, Term

_CMP = {exp.EQ: "=", exp.NEQ: "<>", exp.GT: ">", exp.GTE: ">=", exp.LT: "<", exp.LTE: "<=",
        exp.NullSafeEQ: "not_distinct", exp.NullSafeNEQ: "distinct"}
_FLIP = {"=": "=", "<>": "<>", ">": "<", "<": ">", ">=": "<=", "<=": ">=",
         "not_distinct": "not_distinct", "distinct": "distinct"}
JSON_EXTRACT = (exp.JSONExtract, exp.JSONExtractScalar, exp.JSONBExtract, exp.JSONBExtractScalar)
_JSON_FUNCS = {"json_extract_path", "json_extract_path_text", "jsonb_extract_path", "jsonb_extract_path_text"}
_SIMPLE_FUNCS = {exp.Lower: "lower", exp.Upper: "upper", exp.Length: "length", exp.Trim: "trim", exp.Abs: "abs"}


@dataclass
class Conditions:
    predicates: list[Predicate] = field(default_factory=list)
    comparisons: list[Comparison] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)


class ExpressionAnalyzer:
    def __init__(self, resolve: Callable[[exp.Column], ColumnRef | None]):
        self.resolve = resolve

    # -- conditions --------------------------------------------------------------
    def conditions(self, condition: exp.Expression | None, clause: str, scope: str,
                   required: bool = True, out: Conditions | None = None) -> Conditions:
        out = out or Conditions()
        if condition is not None:
            self._visit(condition, clause, scope, required, False, out)
        return out

    def _visit(self, node, clause, scope, required, negated, out: Conditions) -> None:
        node = _unparen(node)
        if isinstance(node, exp.And):
            # NOT (a AND b) == NOT a OR NOT b: the parts are no longer each required
            for part in node.flatten():
                self._visit(part, clause, scope, required and not negated, negated, out)
            return
        if isinstance(node, exp.Or):
            # NOT (a OR b) == NOT a AND NOT b: each part stays required
            for part in node.flatten():
                self._visit(part, clause, scope, required and negated, negated, out)
            return
        if isinstance(node, exp.Not):
            self._visit(node.this, clause, scope, required, not negated, out)
            return
        if not self._atom(node, clause, scope, required, negated, out):
            out.unparsed.append(("NOT " if negated else "") + node.sql("postgres"))

    def _atom(self, node, clause, scope, required, negated, out: Conditions) -> bool:
        written = node.sql("postgres")
        if isinstance(node, exp.Escape) and isinstance(node.this, (exp.Like, exp.ILike)):
            node = node.this                                            # LIKE ... ESCAPE: keep the pattern
        # sqlglot marks NOT LIKE / NOT IN / NOT BETWEEN / IS NOT NULL with a `negate` flag on the node itself
        negated = negated ^ bool(node.args.get("negate"))

        def pred(term, op, values, neg=None):
            neg = negated if neg is None else neg
            out.predicates.append(Predicate(term, op, tuple(values), neg, clause, scope, required, written))
            return True

        if type(node) in _CMP:
            op = _CMP[type(node)]
            left, right = _unparen(node.this), _unparen(node.expression)
            for quant, other, flipped in ((right, left, False), (left, right, True)):
                if isinstance(quant, exp.Any):                          # x = ANY(...)
                    inner = _unparen(quant.this)
                    items = self.constant(inner)
                    if items is not None and items.kind == "array" and op in ("=", "<>"):
                        term = self.term(other)
                        if term:
                            return pred(term, "in", [Constant(v, repr(v), _kind_of(v)) for v in items.value],
                                        negated ^ (op == "<>"))
                    term, value = self.term(inner), self.constant(other)
                    if term and value is not None and op == "=":       # 'x' = ANY(array_column)
                        return pred(term, "contains", [value])
                    return False
                if isinstance(quant, exp.Anonymous) and str(quant.this).upper() == "ALL" and quant.expressions:
                    items = self.constant(quant.expressions[0])        # x <> ALL(ARRAY[...]) == NOT IN
                    term = self.term(other)
                    if term and items is not None and items.kind == "array" and op == "<>":
                        return pred(term, "in", [Constant(v, repr(v), _kind_of(v)) for v in items.value],
                                    not negated)
                    return False
            if isinstance(left, exp.Tuple) and isinstance(right, exp.Tuple) and op in ("=",) and \
                    len(left.expressions) == len(right.expressions):
                ok = True
                for a, b in zip(left.expressions, right.expressions):  # (a, b) = (1, 2) -> a = 1 AND b = 2
                    ok &= self._atom(exp.EQ(this=a, expression=b), clause, scope, required and not negated,
                                     negated, out)
                return ok
            lt, rt = self.term(left), self.term(right)
            lc, rc = self.constant(left), self.constant(right)
            if lt and rc is not None:
                return pred(lt, op, [rc])
            if rt and lc is not None:
                return pred(rt, _FLIP[op], [lc])
            if lt and rt:
                if negated:
                    return False
                out.comparisons.append(Comparison(lt, op, rt, clause, scope, required, written))
                return True
            return False

        if isinstance(node, (exp.Like, exp.ILike, exp.SimilarTo, exp.RegexpLike, exp.RegexpILike)):
            term, value = self.term(node.this), self.constant(node.expression)
            if term and value is not None and value.kind == "string":
                op = {exp.Like: "like", exp.ILike: "ilike", exp.SimilarTo: "similar",
                      exp.RegexpLike: "regex", exp.RegexpILike: "iregex"}[type(node)]
                return pred(term, op, [value])
            return False

        if isinstance(node, exp.In):
            term = self.term(node.this)
            if term is None or node.args.get("query") is not None or not node.expressions:
                return False
            values = [self.constant(e) for e in node.expressions]
            if any(v is None for v in values):
                return False
            return pred(term, "in", values)

        if isinstance(node, exp.Between):
            term = self.term(node.this)
            lo, hi = self.constant(node.args.get("low")), self.constant(node.args.get("high"))
            if term and lo is not None and hi is not None:
                if node.args.get("symmetric") and _orderable(lo, hi) and lo.value > hi.value:
                    lo, hi = hi, lo
                return pred(term, "between", [lo, hi])
            return False

        if isinstance(node, exp.Is):
            term = self.term(node.this)
            target = node.expression
            if term is None:
                return False
            if isinstance(target, exp.Null):
                return pred(term, "is_null", [])
            if isinstance(target, exp.Boolean):
                return pred(term, "is_true" if target.this else "is_false", [])
            return False

        if isinstance(node, (exp.Column, *JSON_EXTRACT)) or (isinstance(node, exp.Cast) and self.term(node)):
            term = self.term(node)                                     # WHERE active  ->  active IS TRUE
            return pred(term, "is_true", []) if term else False

        json_ops = {"JSONBContainsTopKey": "has_key", "JSONBContainsAnyTopKeys": "has_any_key",
                    "JSONBContainsAllTopKeys": "has_all_keys"}
        if type(node).__name__ in json_ops:
            term, value = self.term(node.this), self.constant(node.expression)
            if term and value is not None:
                return pred(term, json_ops[type(node).__name__], [value])
            return False

        if isinstance(node, (exp.ArrayContainsAll, exp.ArrayContainedBy, exp.ArrayOverlaps)):
            op = {exp.ArrayContainsAll: "contains", exp.ArrayContainedBy: "contained",
                  exp.ArrayOverlaps: "overlaps"}[type(node)]
            lt, rc = self.term(node.this), self.constant(node.expression)
            if lt and rc is not None:
                return pred(lt, op, [rc])
            rt, lc = self.term(node.expression), self.constant(node.this)
            if rt and lc is not None and op in ("contains", "contained"):
                return pred(rt, "contained" if op == "contains" else "contains", [lc])
            return False
        return False

    # -- terms and constants --------------------------------------------------------
    def term(self, node: exp.Expression | None) -> Term | None:
        node = _unparen(node)
        if node is None:
            return None
        sql = node.sql("postgres")
        if isinstance(node, exp.Cast):
            inner = self.term(node.this)
            if inner is None:
                return None
            return replace(inner, cast=node.to.sql("postgres").lower(), sql=sql)
        if isinstance(node, exp.Column):
            ref = self.resolve(node)
            return Term(ref, sql=sql) if ref else None
        if isinstance(node, JSON_EXTRACT):
            keys: list = []
            while isinstance(node, JSON_EXTRACT):
                keys = json_path_keys(node.expression) + keys
                node = _unparen(node.this)
            base = self.term(node)
            if base is None or not base.is_plain:
                return None
            return Term(base.column, tuple(keys), sql=sql)
        if isinstance(node, exp.Anonymous) and str(node.this).lower() in _JSON_FUNCS and node.expressions:
            base = self.term(node.expressions[0])
            keys = [self.constant(a) for a in node.expressions[1:]]
            if base is None or not base.is_plain or any(k is None for k in keys):
                return None
            return Term(base.column, tuple(str(k.value) for k in keys), sql=sql)
        if type(node) in _SIMPLE_FUNCS:
            base = self.term(node.this)
            if base is None or base.function:
                return None
            return replace(base, function=_SIMPLE_FUNCS[type(node)], sql=sql)
        if isinstance(node, exp.Extract):
            base = self.term(node.expression)
            if base is None or base.function:
                return None
            return replace(base, function="extract", function_args=(node.this.name.lower(),), sql=sql)
        if isinstance(node, (exp.TimestampTrunc, exp.DateTrunc)):
            base = self.term(node.this)
            unit = node.args.get("unit")
            if base is None or base.function or unit is None:
                return None
            return replace(base, function="date_trunc", function_args=(unit.name.lower(),), sql=sql)
        if isinstance(node, exp.Coalesce) and len(node.expressions) == 1:
            base, default = self.term(node.this), self.constant(node.expressions[0])
            if base is None or base.function or default is None:
                return None
            return replace(base, function="coalesce", function_args=(default.value,), sql=sql)
        return None

    def constant(self, node: exp.Expression | None) -> Constant | None:
        return constant(node)


def constant(node: exp.Expression | None) -> Constant | None:
    """A literal's value, or None if `node` is not a constant."""
    node = _unparen(node)
    if node is None:
        return None
    sql = node.sql("postgres")
    if isinstance(node, exp.Cast):
        inner = constant(node.this)
        return replace(inner, sql=sql, cast=node.to.sql("postgres").lower()) if inner is not None else None
    if isinstance(node, exp.Neg):
        inner = constant(node.this)
        if inner is not None and inner.kind == "number":
            return Constant(-inner.value, sql, "number")
        return None
    if isinstance(node, exp.Literal):
        if node.is_string:
            return Constant(node.this, sql, "string")
        return Constant(_number(node.this), sql, "number")
    if isinstance(node, (exp.RawString, exp.ByteString)):         # $$text$$, E'text'
        return Constant(node.this, sql, "string")
    if isinstance(node, exp.Boolean):
        return Constant(node.this, sql, "boolean")
    if isinstance(node, exp.Null):
        return Constant(None, sql, "null")
    if isinstance(node, exp.Array):
        items = [constant(e) for e in node.expressions]
        if any(i is None for i in items):
            return None
        return Constant([i.value for i in items], sql, "array")
    return None


def json_path_keys(path) -> list:
    """Keys of a JSON access: ->'a'->0, #>'{a,b}', JSONPath(...)."""
    if isinstance(path, exp.JSONPath):
        return [p.this for p in path.expressions if isinstance(p, (exp.JSONPathKey, exp.JSONPathSubscript))]
    c = constant(path)
    if c is None:
        return []
    if c.kind == "string" and c.value.startswith("{") and c.value.endswith("}"):
        return [k.strip() for k in c.value[1:-1].split(",") if k.strip()]
    return [c.value]


def _unparen(node):
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _number(text: str) -> Any:
    try:
        return int(text)
    except ValueError:
        try:
            return Decimal(text)
        except InvalidOperation:
            return text


def _kind_of(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, Decimal, float)):
        return "number"
    if isinstance(value, list):
        return "array"
    return "string"


def _orderable(a: Constant, b: Constant) -> bool:
    """Both constants can be compared in Python the way Postgres would (C collation)."""
    return a.kind == b.kind and a.kind in ("number", "string") and a.cast == b.cast
