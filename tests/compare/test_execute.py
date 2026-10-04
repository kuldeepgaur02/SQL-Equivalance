"""Step 7 on real queries in Postgres."""
import pytest

from cexgen.compare import BOTH_EMPTY, DIFFER, ERROR, ORDER, ROWS, SAME, compare, run_pair
from cexgen.db import SchemaWorkspace

pytestmark = pytest.mark.db

DDL = """
CREATE TABLE emp (name text, salary int, dept int, bonus numeric(6,2));
INSERT INTO emp VALUES ('ann', 100, 1, 1.50), ('bob', 100, 1, NULL), ('cy', 200, 2, 0), ('cy', 200, 2, 0);
"""


@pytest.fixture(scope="module")
def ws(settings):
    with SchemaWorkspace(DDL, settings) as w:
        yield w


def outcome(ws, q1, q2):
    r1, r2 = run_pair(ws.conn, q1, q2, 1000)
    c = compare(r1, r2)
    return c.outcome, c.kind, c


def test_same_rows_different_query(ws):
    assert outcome(ws, "SELECT name FROM emp WHERE salary > 150", "SELECT name FROM emp WHERE salary >= 200")[0] == SAME


def test_duplicates_and_distinct(ws):
    o, k, c = outcome(ws, "SELECT name FROM emp", "SELECT DISTINCT name FROM emp")
    assert (o, k) == (DIFFER, ROWS) and c.only_in_q1 == ((("cy",), 1),)        # one extra copy of cy


def test_null_stays_null(ws):
    o, k, _ = outcome(ws, "SELECT bonus FROM emp", "SELECT coalesce(bonus, 0) FROM emp")
    assert (o, k) == (DIFFER, ROWS)


def test_integer_vs_numeric_same_value(ws):
    o, _, c = outcome(ws, "SELECT salary FROM emp", "SELECT salary * 1.0 FROM emp")
    assert o == SAME and any("integer" in n and "numeric" in n for n in c.notes)


def test_ties_do_not_count_but_real_order_does(ws):
    assert outcome(ws, "SELECT name FROM emp ORDER BY salary, name",
                   "SELECT name FROM emp ORDER BY salary, name DESC")[1] == ORDER       # name decides within ties
    assert outcome(ws, "SELECT name, salary FROM emp ORDER BY salary",
                   "SELECT name, salary FROM emp ORDER BY salary, name DESC")[0] == SAME  # Q1 leaves ties open
    assert outcome(ws, "SELECT name FROM emp ORDER BY salary DESC",
                   "SELECT name FROM emp ORDER BY salary")[1] == ORDER


def test_order_by_a_column_not_selected(ws):
    o, k, c = outcome(ws, "SELECT name FROM emp ORDER BY salary", "SELECT name FROM emp ORDER BY salary DESC")
    assert (o, k) == (DIFFER, ORDER) and c.q1.keys is not None


def test_run_time_error_on_one_side(ws):
    o, k, c = outcome(ws, "SELECT 100 / (salary - 100) FROM emp", "SELECT 100 / nullif(salary - 100, 0) FROM emp")
    assert (o, k) == (DIFFER, ERROR) and c.q1.error["sqlstate"] == "22012" and len(c.q2.rows) == 4


def test_both_empty(ws):
    assert outcome(ws, "SELECT name FROM emp WHERE salary > 1000", "SELECT name FROM emp WHERE dept = 9")[0] \
        == BOTH_EMPTY


def test_both_queries_see_the_same_snapshot(ws):
    r1, r2 = run_pair(ws.conn, "SELECT now(), txid_current_if_assigned()", "SELECT now(), 1", 10)
    assert r1.rows[0][0] == r2.rows[0][0]                  # the same transaction: the same now()


def test_result_cap(ws):
    r1, _ = run_pair(ws.conn, "SELECT g FROM generate_series(1, 50) g", "SELECT 1", 10)
    assert len(r1.rows) == 10 and r1.truncated
