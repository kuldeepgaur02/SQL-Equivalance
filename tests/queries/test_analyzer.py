"""Q1 / Q2 analysis against a real schema: Postgres checks, sqlglot breaks down."""
import pytest

from cexgen.db import SchemaWorkspace
from cexgen.errors import QueryError
from cexgen.queries import ColumnRef, analyze_query, pair_notes
from cexgen.queries.describe import describe_predicate
from cexgen.schema import QName, read_schema

pytestmark = pytest.mark.db

SCHEMA = """
CREATE TYPE status AS ENUM ('new', 'paid');
CREATE TABLE users (id int PRIMARY KEY, email text NOT NULL, age int, active bool);
CREATE TABLE orders (id int PRIMARY KEY, user_id int REFERENCES users, amount numeric, status status,
                     meta jsonb, tags text[]);
CREATE SCHEMA app;
CREATE TABLE app.users (id int, nickname text);
CREATE TABLE "Mixed" ("Id" int, "Name" text);
CREATE VIEW paid_orders AS SELECT * FROM orders WHERE status = 'paid';
"""


@pytest.fixture(scope="module")
def env(settings):
    with SchemaWorkspace(SCHEMA, settings) as ws:
        yield ws, read_schema(ws.conn, ws.inventory)


def run(env, sql, label="Q1"):
    ws, model = env
    return analyze_query(label, sql, model, ws.conn)


def preds(info):
    return [describe_predicate(p) + ("" if p.required else " ?") for p in info.predicates]


def test_simple_filters_and_result(env):
    q = run(env, "SELECT o.id, u.email FROM orders o JOIN users u ON u.id = o.user_id WHERE o.amount > 100")
    assert [(r.name, r.type) for r in q.result] == [("id", "integer"), ("email", "text")]
    assert q.tables == (QName("public", "orders"), QName("public", "users"))
    assert preds(q) == ["public.orders.amount > 100"]
    assert [c.sql for c in q.comparisons] == ["u.id = o.user_id"]


def test_unqualified_columns_and_search_path(env):
    q = run(env, "SELECT email FROM users WHERE age >= 18 AND active")
    assert preds(q) == ["public.users.age >= 18", "public.users.active IS TRUE"]


def test_schema_qualified_and_case_sensitive_names(env):
    q = run(env, 'SELECT nickname FROM app.users WHERE id = 1')
    assert q.predicates[0].term.column == ColumnRef(QName("app", "users"), "id")
    q = run(env, 'SELECT "Name" FROM "Mixed" WHERE "Id" = 2')
    assert q.predicates[0].term.column == ColumnRef(QName("public", "Mixed"), "Id")


def test_cte_and_derived_columns_traced_to_base_table(env):
    q = run(env, """
        WITH big AS (SELECT id AS oid, amount AS amt FROM orders WHERE amount > 10)
        SELECT d.x FROM (SELECT oid AS x, amt FROM big) d WHERE d.amt < 50 AND d.x = 3
    """)
    found = {(str(p.term.column), p.op, p.scope) for p in q.predicates}
    assert ("public.orders.amount", ">", "cte:big") in found
    assert ("public.orders.amount", "<", "main") in found
    assert ("public.orders.id", "=", "main") in found


def test_outer_join_on_is_not_a_result_filter(env):
    q = run(env, "SELECT u.id FROM users u LEFT JOIN orders o ON o.user_id = u.id AND o.status = 'paid'")
    [p] = q.predicates
    assert p.clause == "on" and not p.required
    assert "left_join" in q.features and q.joins[0].kind == "left"


def test_json_paths_from_select_and_where(env):
    q = run(env, "SELECT meta->'a'->>'b' FROM orders WHERE (meta->>'k')::int > 5 AND meta ? 'z'")
    meta = ColumnRef(QName("public", "orders"), "meta")
    assert set(q.json_paths[meta]) == {("a", "b"), ("k",)}             # paths read from the document
    assert [(p.op, p.values[0].value) for p in q.predicates if p.op == "has_key"] == [("has_key", "z")]


def test_views_expand_to_base_tables(env):
    q = run(env, "SELECT id FROM paid_orders WHERE amount > 5")
    assert q.views == (QName("public", "paid_orders"),) and q.tables == (QName("public", "orders"),)
    assert preds(q) == ["public.paid_orders.amount > 5"]


def test_features(env):
    q = run(env, """
        SELECT DISTINCT u.id, count(*) FROM users u
        WHERE u.id NOT IN (SELECT user_id FROM orders) AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.user_id = u.id)
          AND coalesce(u.age, 0) > 1 AND u.email IS NOT NULL
        GROUP BY u.id HAVING count(*) > 0
        UNION ALL SELECT 1, 2 ORDER BY 1 LIMIT 5
    """)
    for f in ("distinct", "not_in_subquery", "not_exists", "coalesce", "null_test", "group_by", "having",
              "union_all", "order_by", "limit", "count_star", "aggregate"):
        assert f in q.features, f
    assert "count(*) > 0" in " ".join(q.unparsed).lower()


def test_volatile_functions_are_flagged(env):
    q = run(env, "SELECT id FROM orders WHERE random() < 0.5")
    assert q.volatile_functions == ("random",) and "volatile" in q.features
    assert "may not be a real counterexample" in q.warnings[0]
    assert run(env, "SELECT lower(email), now() FROM users").volatile_functions == ()   # stable / immutable


@pytest.mark.parametrize("sql", [
    "DELETE FROM orders",
    "WITH d AS (DELETE FROM orders RETURNING id) SELECT * FROM d",
    "SELECT * INTO copy FROM orders",
    "INSERT INTO users VALUES (1, 'a', 1, true)",
])
def test_writing_queries_are_rejected(env, sql):
    with pytest.raises(QueryError, match="read-only"):
        run(env, sql)


def test_postgres_rejection_points_at_the_problem(env):
    with pytest.raises(QueryError) as e:
        run(env, "SELECT id\nFROM orders\nWHERE amout > 1")
    assert e.value.sqlstate == "42703" and e.value.line == 3 and "amout" in e.value.excerpt


def test_sqlglot_failure_degrades_gracefully(env, monkeypatch):
    import cexgen.queries.analyzer as analyzer
    import sqlglot

    def boom(*a, **k):
        raise sqlglot.errors.ParseError("unsupported")
    monkeypatch.setattr(analyzer, "ParsedQuery", boom)
    q = run(env, "SELECT id FROM orders WHERE random() > 0.1")
    assert not q.parsed and q.result and q.volatile_functions == ("random",)
    assert "filters are unknown" in q.warnings[0]


def test_pair_notes(env):
    a = run(env, "SELECT id, email FROM users")
    b = run(env, "SELECT id FROM users", "Q2")
    assert "any non-empty result already differs" in pair_notes(a, b, False)[0]
    c = run(env, "SELECT id::bigint, email FROM users", "Q2")
    assert "column 1: Q1 gives integer, Q2 gives bigint" in pair_notes(a, c, False)[0]


def test_percent_sign_in_query_is_not_a_parameter(env):
    q = run(env, "SELECT id FROM users WHERE email LIKE '%@x.com'")
    assert q.patterns == ("%@x.com",)
