"""The shared expression analyser, without a database: columns resolve to table t."""
from decimal import Decimal

import pytest
import sqlglot

from cexgen.queries import ColumnRef, ExpressionAnalyzer
from cexgen.schema import QName

T = QName("public", "t")


def analyse(condition: str):
    tree = sqlglot.parse_one(f"SELECT 1 FROM t WHERE {condition}", read="postgres").args["where"].this
    return ExpressionAnalyzer(lambda col: ColumnRef(T, col.name)).conditions(tree, "where", "main")


def one(condition: str):
    found = analyse(condition)
    assert len(found.predicates) == 1, (found.predicates, found.unparsed)
    p = found.predicates[0]
    return p.term.column.column, p.op, [v.value for v in p.values], p.negated, p.required


@pytest.mark.parametrize("cond, expected", [
    ("a > 100", ("a", ">", [100], False, True)),
    ("100 < a", ("a", ">", [100], False, True)),                      # constant on the left: flipped
    ("a <> 'x'", ("a", "<>", ["x"], False, True)),
    ("a = -5", ("a", "=", [-5], False, True)),
    ("a = 1.50", ("a", "=", [Decimal("1.50")], False, True)),
    ("a = TRUE", ("a", "=", [True], False, True)),
    ("a = NULL", ("a", "=", [None], False, True)),                    # kept as written: never true in SQL
    ("a IS DISTINCT FROM 1", ("a", "distinct", [1], False, True)),
    ("a IS NOT DISTINCT FROM 1", ("a", "not_distinct", [1], False, True)),
    ("a LIKE 'x%'", ("a", "like", ["x%"], False, True)),
    ("a NOT LIKE 'x%'", ("a", "like", ["x%"], True, True)),
    ("a ILIKE 'X%'", ("a", "ilike", ["X%"], False, True)),
    ("a LIKE 'x!%' ESCAPE '!'", ("a", "like", ["x!%"], False, True)),
    ("a SIMILAR TO 'x%'", ("a", "similar", ["x%"], False, True)),
    ("a ~ '^x'", ("a", "regex", ["^x"], False, True)),
    ("a !~ '^x'", ("a", "regex", ["^x"], True, True)),
    ("a IN (1, 2, NULL)", ("a", "in", [1, 2, None], False, True)),
    ("a NOT IN (1, 2)", ("a", "in", [1, 2], True, True)),
    ("a = ANY(ARRAY[1, 2])", ("a", "in", [1, 2], False, True)),
    ("a <> ALL(ARRAY[1, 2])", ("a", "in", [1, 2], True, True)),
    ("3 = ANY(arr)", ("arr", "contains", [3], False, True)),
    ("a BETWEEN 1 AND 5", ("a", "between", [1, 5], False, True)),
    ("a NOT BETWEEN 1 AND 5", ("a", "between", [1, 5], True, True)),
    ("a BETWEEN SYMMETRIC 5 AND 1", ("a", "between", [1, 5], False, True)),
    ("a IS NULL", ("a", "is_null", [], False, True)),
    ("a IS NOT NULL", ("a", "is_null", [], True, True)),
    ("a IS TRUE", ("a", "is_true", [], False, True)),
    ("a IS NOT FALSE", ("a", "is_false", [], True, True)),
    ("active", ("active", "is_true", [], False, True)),
    ("NOT active", ("active", "is_true", [], True, True)),
    ("j ? 'k'", ("j", "has_key", ["k"], False, True)),
    ("j ?| ARRAY['a', 'b']", ("j", "has_any_key", [["a", "b"]], False, True)),
    ("arr && ARRAY[1]", ("arr", "overlaps", [[1]], False, True)),
    ("a = $$x$$", ("a", "=", ["x"], False, True)),
])
def test_atoms(cond, expected):
    assert one(cond) == expected


def test_terms_keep_json_path_cast_and_function():
    p = analyse("(j->'a'->>'b')::int > 5").predicates[0]
    assert (p.term.json_path, p.term.cast) == (("a", "b"), "int")
    p = analyse("j #>> '{x,y}' = 'v'").predicates[0]
    assert p.term.json_path == ("x", "y")
    p = analyse("lower(a) = 'x'").predicates[0]
    assert p.term.function == "lower"
    p = analyse("extract(year FROM d) = 2024").predicates[0]
    assert (p.term.function, p.term.function_args) == ("extract", ("year",))
    p = analyse("date_trunc('month', d) = '2024-01-01'").predicates[0]
    assert (p.term.function, p.term.function_args) == ("date_trunc", ("month",))
    p = analyse("coalesce(a, 0) = 0").predicates[0]
    assert (p.term.function, p.term.function_args) == ("coalesce", (0,))


def test_constants_keep_casts():
    p = analyse("d = DATE '2024-01-01'").predicates[0]
    assert (p.values[0].value, p.values[0].cast) == ("2024-01-01", "date")
    p = analyse("a = '5'::int").predicates[0]
    assert p.values[0].cast == "int"


def test_required_vs_optional():
    found = analyse("a > 1 AND (b = 2 OR c = 3) AND NOT (d = 4 OR e = 5) AND NOT (f = 6 AND g = 7)")
    req = {p.term.column.column: (p.required, p.negated) for p in found.predicates}
    assert req["a"] == (True, False)
    assert req["b"] == (False, False) and req["c"] == (False, False)
    assert req["d"] == (True, True) and req["e"] == (True, True)      # NOT (d OR e) = NOT d AND NOT e
    assert req["f"] == (False, True) and req["g"] == (False, True)    # NOT (f AND g) = NOT f OR NOT g


def test_column_to_column_and_tuples():
    found = analyse("lo < hi AND (a, b) = (1, 2)")
    assert [c.op for c in found.comparisons] == ["<"]
    assert [(p.term.column.column, p.values[0].value) for p in found.predicates] == [("a", 1), ("b", 2)]


def test_what_cannot_be_broken_down_is_kept_as_text():
    found = analyse("a = b + 1 AND a > (SELECT max(x) FROM u) AND EXISTS (SELECT 1 FROM u)")
    assert found.predicates == [] and len(found.unparsed) == 3
