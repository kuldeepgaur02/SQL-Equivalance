"""Step 7's comparison rules, on hand-made results (no database)."""
import datetime as dt
from decimal import Decimal

import pytest

from cexgen.compare import (BOTH_EMPTY, DIFFER, ERROR, ORDER, ROWS, SAME, QueryRun, compare, nondeterminism,
                            order_spec, value_key)

INT, NUM, TEXT, BPCHAR, JSONB = 23, 1700, 25, 1042, 3802


def run(label, rows, oids=(INT,), ordered=False, keys=None, error=None, types=None):
    oids = tuple(oids)
    return QueryRun(label, tuple(f"c{i}" for i in range(len(oids))), tuple(types or [str(o) for o in oids]), oids,
                    tuple(tuple(r) for r in rows), keys, ordered, error)


# -- values -----------------------------------------------------------------------------------
@pytest.mark.parametrize("a, oa, b, ob, equal", [
    (5, INT, Decimal("5.0"), NUM, True),              # numbers by exact value
    (2, INT, Decimal("2.5"), NUM, False),
    (0.1, 701, Decimal("0.1"), NUM, True),            # float by its shortest exact decimal
    (0.1, 701, Decimal("0.10000001"), NUM, False),    # exact: no tolerance
    ("a    ", BPCHAR, "a", TEXT, True),               # char(n) padding does not count
    ("a ", TEXT, "a", TEXT, False),                   # ...but in text it does
    ("5", TEXT, 5, INT, False),                       # different kinds never match
    (True, 16, 1, INT, False),
    (None, INT, None, TEXT, True),                    # NULL only equals NULL
    (None, INT, 0, INT, False),
    (dt.date(2024, 1, 1), 1082, dt.datetime(2024, 1, 1), 1114, True),
    ({"a": 1, "b": 2}, JSONB, {"b": 2, "a": 1}, JSONB, True),
    ([1, None], 1007, [1, None], 1007, True),
])
def test_value_rules(a, oa, b, ob, equal):
    assert (value_key(a, oa) == value_key(b, ob)) is equal


# -- outcomes --------------------------------------------------------------------------------
def test_bag_ignores_order_but_counts_duplicates():
    assert compare(run("Q1", [[1], [2]]), run("Q2", [[2], [1]])).outcome == SAME
    c = compare(run("Q1", [[1], [1], [2]]), run("Q2", [[1], [2]]))
    assert (c.outcome, c.kind) == (DIFFER, ROWS) and c.only_in_q1 == (((1,), 1),) and c.only_in_q2 == ()
    assert c.counterexample


def test_both_empty_and_column_counts():
    assert compare(run("Q1", []), run("Q2", [])).outcome == BOTH_EMPTY
    assert compare(run("Q1", [], (INT, INT)), run("Q2", [])).outcome == BOTH_EMPTY
    c = compare(run("Q1", [[1, 2]], (INT, INT)), run("Q2", [[1]]))
    assert (c.outcome, c.kind) == (DIFFER, ROWS) and "column(s)" in c.reason


def test_type_difference_is_only_a_note():
    c = compare(run("Q1", [[5]], (INT,), types=["integer"]), run("Q2", [[Decimal("5.00")]], (NUM,), types=["numeric"]))
    assert c.outcome == SAME and "Q1 gives integer, Q2 gives numeric" in c.notes[0]


def test_errors():
    err = lambda code: {"sqlstate": code, "message": "x"}  # noqa: E731
    one = compare(run("Q1", [], error=err("22012")), run("Q2", [[1]]))
    assert (one.outcome, one.kind) == (DIFFER, ERROR) and "Q1 fails (22012" in one.reason
    assert compare(run("Q1", [], error=err("22012")), run("Q2", [], error=err("22012"))).outcome == SAME
    assert compare(run("Q1", [], error=err("22012")), run("Q2", [], error=err("22003"))).kind == ERROR


def test_order_only_when_both_ask_for_it():
    asc, desc = [[1], [2]], [[2], [1]]
    keys_asc, keys_desc = ((1,), (2,)), ((2,), (1,))
    both = compare(run("Q1", asc, ordered=True, keys=keys_asc), run("Q2", desc, ordered=True, keys=keys_desc))
    assert (both.outcome, both.kind) == (DIFFER, ORDER) and "different order" in both.reason
    one = compare(run("Q1", asc, ordered=True, keys=keys_asc), run("Q2", desc))
    assert one.outcome == SAME                                         # only Q1 asks for an order


def test_rows_tied_on_the_sort_key_are_interchangeable():
    # ORDER BY salary: Ann and Bob both earn 100, so either may come first
    q1 = run("Q1", [["ann", 100], ["bob", 100], ["cy", 200]], (TEXT, INT), ordered=True,
             keys=((100,), (100,), (200,)))
    q2 = run("Q2", [["bob", 100], ["ann", 100], ["cy", 200]], (TEXT, INT), ordered=True,
             keys=((100,), (100,), (200,)))
    assert compare(q1, q2).outcome == SAME
    q3 = run("Q2", [["cy", 200], ["ann", 100], ["bob", 100]], (TEXT, INT), ordered=True,
             keys=((200,), (100,), (100,)))                               # ORDER BY salary DESC
    assert compare(q1, q3).kind == ORDER


def test_nondeterministic_difference_is_not_a_counterexample():
    c = compare(run("Q1", [[1]]), run("Q2", [[2]]), ["Q1: LIMIT / OFFSET without ORDER BY"])
    assert c.outcome == DIFFER and not c.counterexample


# -- reading ORDER BY and nondeterminism ------------------------------------------------------
def test_order_spec():
    assert not order_spec("SELECT a FROM t").ordered
    assert order_spec("SELECT a, b FROM t ORDER BY 2").key_columns == (1,)
    assert order_spec("SELECT a AS x FROM t ORDER BY x DESC").key_columns == (0,)
    assert order_spec("SELECT t.a FROM t ORDER BY t.a").key_columns == (0,)
    spec = order_spec("SELECT a FROM t ORDER BY b, a")
    assert spec.key_columns == (1, 0) and "_cex_sort_key_0" in spec.keyed_sql
    assert order_spec("SELECT a FROM t UNION SELECT b FROM u ORDER BY 1").key_columns == (0,)
    assert order_spec("SELECT * FROM t ORDER BY b").note is not None     # cannot add keys after *


def test_nondeterminism():
    assert nondeterminism("SELECT a FROM t LIMIT 1")
    assert nondeterminism("SELECT a FROM t ORDER BY a LIMIT 1") == []
    assert nondeterminism("SELECT * FROM (SELECT a FROM t OFFSET 2) s ORDER BY a")
    assert nondeterminism("SELECT DISTINCT ON (a) a, b FROM t")
    assert nondeterminism("SELECT a FROM t") == []


def test_same_values_in_another_column_order():
    from cexgen.compare import COLUMNS
    q1 = run("Q1", [[1, "a", 10], [2, "b", 20]], (INT, TEXT, INT))
    q2 = run("Q2", [[1, 10, "a"], [2, 20, "b"]], (INT, INT, TEXT))
    c = compare(q1, q2)
    assert (c.outcome, c.kind) == (DIFFER, COLUMNS) and "Q1 column 2 is Q2 column 3" in c.reason
    # same column bags but rows pair up differently: a real difference, not a column swap
    q3 = run("Q2", [[1, 20, "a"], [2, 10, "b"]], (INT, INT, TEXT))
    assert compare(q1, q3).kind == ROWS
