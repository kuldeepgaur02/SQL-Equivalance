"""Make a base row satisfy the schema's understood rules (prevent, don't repair).

For each column, the required predicates of every understood rule on it
(CHECK, domain CHECK) are collected, and the first candidate value that
satisfies all of them and fits the column is kept:

    current value -> each predicate's witness -> those applied in turn (a few passes)

e.g. code varchar(8) CHECK (code LIKE 'AC-%' AND length(code) = 6):
    'a' -> 'AC-' (LIKE) -> 'AC-aaa' (length) -> satisfies both.

Then column-to-column rules (lo < hi) and partition bounds. Whatever cannot be
satisfied is reported, and Step 6's repair loop handles it.
"""
from __future__ import annotations

from fractions import Fraction
from typing import Any

from ..queries.model import Predicate
from ..queries.rules import Rule
from ..schema.model import Column, SchemaModel, Table
from ..schema.names import QName
from ..schema.partitions import BoundValue
from ..schema.types import Family, TypeInfo
from .values import coerce, compare, fits, from_like, like_matches, predecessor, sort_key, successor

_SUPPORTED_FUNCS = (None, "lower", "upper", "length", "trim", "abs")
_NEGATE = {"=": "<>", "<>": "=", ">": "<=", "<=": ">", "<": ">=", ">=": "<", "in": "not_in", "between": "not_between",
           "like": "not_like", "ilike": "not_ilike", "is_null": "not_null", "is_true": "not_true",
           "is_false": "not_false", "distinct": "not_distinct", "not_distinct": "distinct"}


# -- one predicate ---------------------------------------------------------------------
def effective_op(p: Predicate) -> str:
    return _NEGATE.get(p.op, f"not_{p.op}") if p.negated else p.op


def holds(p: Predicate, value: Any, column: Column) -> bool:
    """Is the predicate TRUE for this value? (NULL / unknown counts as not true.)"""
    op = effective_op(p)
    if op == "is_null":
        return value is None
    if op == "not_null":
        return value is not None
    if value is None:
        return False
    value = _apply_function(p, value)
    target = _target_column(p, column)
    vals = [_coerce_const(c.value, p, target) for c in p.values]
    if op in ("is_true", "not_false"):
        return value is True
    if op in ("not_true", "is_false"):
        return value is False
    if op in ("like", "ilike", "not_like", "not_ilike"):
        m = like_matches(value, str(vals[0]), case_insensitive="ilike" in op)
        return m if not op.startswith("not") else not m
    if op in ("in", "not_in"):
        inside = any(compare(value, v, target) == 0 for v in vals if v is not None)
        return inside if op == "in" else (not inside and None not in vals)
    if op in ("between", "not_between"):
        lo, hi = compare(value, vals[0], target), compare(value, vals[1], target)
        if lo is None or hi is None:
            return False
        inside = lo >= 0 and hi <= 0
        return inside if op == "between" else not inside
    c = compare(value, vals[0], target) if vals and vals[0] is not None else None
    if op in ("=", "not_distinct"):
        return c == 0
    if op in ("<>", "distinct"):
        return c is not None and c != 0
    return c is not None and {">": c > 0, ">=": c >= 0, "<": c < 0, "<=": c <= 0}.get(op, False)


def witness(p: Predicate, column: Column, current: Any) -> Any:
    """A value of `column` for which `p` holds, close to `current`. None if unknown."""
    op = effective_op(p)
    target = _target_column(p, column)
    vals = [_coerce_const(c.value, p, target) for c in p.values]
    if p.term.function == "length":
        return _length_witness(op, vals, current, column)
    if op == "is_null":
        return None
    if op == "not_null":
        return current
    if op in ("is_true", "not_false"):
        return True
    if op in ("not_true", "is_false"):
        return False
    v: Any = None
    if op in ("=", "not_distinct", ">=", "<=", "between"):
        v = vals[0]
    elif op == ">":
        v = successor(vals[0], target)
    elif op == "<":
        v = predecessor(vals[0], target)
    elif op in ("<>", "distinct"):
        v = current if compare(current, vals[0], target) not in (0, None) else successor(vals[0], target)
    elif op == "in":
        v = next((x for x in vals if x is not None), None)
    elif op == "not_in":
        v = _outside(vals, current, target)
    elif op == "not_between":
        v = successor(vals[1], target)
    elif op in ("like", "ilike"):
        v = from_like(str(vals[0]))
        if isinstance(current, str) and like_matches(current, str(vals[0]), op == "ilike"):
            v = current
    elif op in ("not_like", "not_ilike"):
        v = next((s for s in (current, "zz", "z9", "#") if isinstance(s, str)
                  and not like_matches(s, str(vals[0]), op == "not_ilike")), None)
    if isinstance(v, str) and p.term.function in ("lower", "upper"):
        v = v.lower() if p.term.function == "lower" else v.upper()
    return v


# -- a whole row -----------------------------------------------------------------------
def solve_row(table: Table, row: dict[str, Any], rules: tuple[Rule, ...]) -> list[str]:
    """Change `row` in place so the understood rules hold. Returns the rules not satisfied."""
    unsatisfied: list[str] = []
    by_column: dict[str, list[tuple[Rule, Predicate]]] = {}
    for rule in rules:
        if not rule.understood:
            unsatisfied.append(f"{rule.name} (not understood: {rule.expression})")
            continue
        for p in rule.predicates:
            if p.term.function in _SUPPORTED_FUNCS and not p.term.json_path and p.term.column.column in row:
                by_column.setdefault(p.term.column.column, []).append((rule, p))

    for name, items in by_column.items():
        column = table.columns[name]
        preds = [p for _, p in items]
        value = _best(column, preds, row[name])
        if value is _NONE:
            unsatisfied += sorted({f"{r.name}: {r.expression}" for r, _ in items})
        else:
            row[name] = value

    for rule in rules:
        if rule.understood:
            for cmp in rule.comparisons:
                if not _fix_comparison(table, row, cmp, by_column):
                    unsatisfied.append(f"{rule.name}: {rule.expression}")
    return sorted(set(unsatisfied))


_NONE = object()


def _best(column: Column, preds: list[Predicate], current: Any) -> Any:
    def ok(v):
        return fits(v, column) and all(holds(p, v, column) for p in preds)

    if ok(current):
        return current
    candidates = []
    v = current
    for _ in range(4):                       # apply each predicate's witness in turn, a few passes
        for p in preds:
            if not holds(p, v, column):
                w = witness(p, column, v)
                if w is not None or effective_op(p) == "is_null":
                    v = w
        candidates.append(v)
    candidates += [witness(p, column, current) for p in preds]
    for c in candidates:
        if ok(c):
            return c
    return _NONE


def _fix_comparison(table: Table, row: dict, cmp, by_column) -> bool:
    if not (cmp.left.is_plain and cmp.right.is_plain):
        return True
    a, b = cmp.left.column.column, cmp.right.column.column
    if a not in row or b not in row:
        return True
    ca, cb = table.columns[a], table.columns[b]

    def holds_now() -> bool:
        c = compare(row[a], row[b], ca)
        return c is not None and {"=": c == 0, "<>": c != 0, "<": c < 0, "<=": c <= 0, ">": c > 0,
                                  ">=": c >= 0}.get(cmp.op, True)

    if holds_now():
        return True
    own = lambda name, v: all(holds(p, v, table.columns[name]) for _, p in by_column.get(name, ()))  # noqa: E731
    options = {
        "<": [(b, successor(row[a], cb)), (a, predecessor(row[b], ca))],
        "<=": [(b, row[a]), (a, row[b])],
        ">": [(b, predecessor(row[a], cb)), (a, successor(row[b], ca))],
        ">=": [(b, row[a]), (a, row[b])],
        "=": [(b, row[a]), (a, row[b])],
        "<>": [(b, successor(row[a], cb)), (a, successor(row[b], ca))],
    }.get(cmp.op, [])
    for name, value in options:
        if value is not None and fits(value, table.columns[name]) and own(name, value):
            old = row[name]
            row[name] = value
            if holds_now():
                return True
            row[name] = old
    return False


# -- partition bounds ------------------------------------------------------------------------
def place_in_partition(model: SchemaModel, table: Table, row: dict[str, Any]) -> str | None:
    """Set the partition key so the row fits a partition (recursing into sub-partitions).
    Returns a problem description, or None when the row fits."""
    current = table
    while current.partitioning is not None:
        part = current.partitioning
        if not part.partitions:
            return f"{current.qname} has no partitions"
        if part.strategy == "hash":
            # Every value hashes into some partition only if the partitions cover all remainders.
            covered = sum(Fraction(1, p.bound.modulus) for p in part.partitions if p.bound.kind == "hash")
            if covered >= 1 or part.has_default:
                return None
            return f"{current.qname}: hash partitions do not cover every remainder; left to repair"
        if any(k is None for k in part.key):
            return f"{current.qname} is partitioned on an expression ({part.key_sql}); left to repair"
        key = [table.columns[k] for k in part.key]
        chosen = next((p for p in part.partitions if _fits_bound(p.bound, key, row)), None)
        if chosen is None:
            chosen = next((p for p in part.partitions if p.bound.kind != "default"), part.partitions[0])
            if not _move_into(chosen.bound, key, row):
                if not part.has_default:
                    return f"could not place the row in a partition of {current.qname}"
                chosen = next(p for p in part.partitions if p.bound.kind == "default")
        current = model.tables[chosen.table]
    return None


def _fits_bound(bound, key: list[Column], row: dict) -> bool:
    if bound.kind == "default":
        return False                         # prefer a real partition; DEFAULT is the last resort
    if bound.kind == "list":
        v = row[key[0].name]
        return any((b.kind == "null" and v is None) or
                   (b.kind == "literal" and compare(v, coerce(b.text, key[0]), key[0]) == 0) for b in bound.values)
    if bound.kind == "range":
        lo = tuple(_bound_key(b, c) for b, c in zip(bound.lower, key))
        hi = tuple(_bound_key(b, c) for b, c in zip(bound.upper, key))
        v = tuple((0, _safe_key(row[c.name], c)) for c in key)
        try:
            return lo <= v < hi
        except TypeError:
            return False
    return False


def _move_into(bound, key: list[Column], row: dict) -> bool:
    if bound.kind == "list":
        first = bound.values[0]
        row[key[0].name] = None if first.kind == "null" else coerce(first.text, key[0])
        return True
    if bound.kind == "range":
        if all(b.kind == "literal" for b in bound.lower):          # lower bound is inclusive
            for b, c in zip(bound.lower, key):
                row[c.name] = coerce(b.text, c)
            return True
        first = bound.upper[0]
        if first.kind == "literal":                                # MINVALUE .. x: just below x
            row[key[0].name] = predecessor(coerce(first.text, key[0]), key[0])
            return row[key[0].name] is not None
    return False


def _bound_key(b: BoundValue, column: Column):
    if b.kind == "minvalue":
        return (-1, 0)
    if b.kind == "maxvalue":
        return (1, 0)
    return (0, _safe_key(coerce(b.text, column), column))


def _safe_key(value, column):
    return sort_key(value, column)


# -- helpers ---------------------------------------------------------------------------------
def _apply_function(p: Predicate, value: Any) -> Any:
    f = p.term.function
    if f == "length":
        return len(str(value).rstrip(" ")) if value is not None else None
    if f in ("lower", "upper") and isinstance(value, str):
        return value.lower() if f == "lower" else value.upper()
    if f == "trim" and isinstance(value, str):
        return value.strip()
    if f == "abs":
        try:
            return abs(value)
        except TypeError:
            return value
    return value


def _target_column(p: Predicate, column: Column) -> Column:
    """length(x) compares integers, whatever x's type is."""
    if p.term.function == "length":
        int_type = TypeInfo(QName("pg_catalog", "int4"), "integer", "int4", Family.INTEGER,
                            min_value=0, max_value=2147483647)
        return Column(column.name, column.position, int_type, False)
    return column


def _coerce_const(value: Any, p: Predicate, column: Column) -> Any:
    return coerce(value, column) if not isinstance(value, list) else value


def _length_witness(op: str, vals: list, current: Any, column: Column) -> Any:
    n = int(vals[0]) if vals and vals[0] is not None else 1
    want = {"=": n, ">=": n, "<=": n, ">": n + 1, "<": max(n - 1, 0), "<>": n + 1, "between": n}.get(op)
    if want is None:
        return None
    base = current if isinstance(current, str) else ""
    return (base + "a" * want)[:want]


def _outside(vals: list, current: Any, column: Column) -> Any:
    if current is not None and all(compare(current, v, column) != 0 for v in vals if v is not None):
        return current
    if column.type.family == Family.ENUM:
        return next((label for label in column.type.enum_labels if label not in vals), None)
    known = [v for v in vals if v is not None]
    if column.type.family in (Family.INTEGER, Family.NUMERIC, Family.FLOAT) and known:
        top = max(known, key=lambda v: float(v))
        return successor(top, column)
    i = 0
    while f"x{i}" in vals:
        i += 1
    return f"x{i}"
