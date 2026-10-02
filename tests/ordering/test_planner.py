"""The insert plan for each situation agreed in the Step 4 discussion."""
import pytest

from cexgen.db import SchemaWorkspace
from cexgen.ordering import DEFERRED, NOT_NEEDED, NULL, NULL_THEN_UPDATE, PARENT, SELF, plan_inserts
from cexgen.queries import QueryPair, analyze_query, table_rules
from cexgen.schema import QName, read_schema

pytestmark = pytest.mark.db


def q(name):
    return QName("public", name)


@pytest.fixture
def plan_for(settings):
    """plan_for(ddl, q1, q2=None, scope='queries') -> InsertPlan"""
    def build(ddl, q1=None, q2=None, scope="queries"):
        with SchemaWorkspace(ddl, settings) as ws:
            model = read_schema(ws.conn, ws.inventory)
            pair = None
            if q1:
                pair = QueryPair(analyze_query("Q1", q1, model, ws.conn), analyze_query("Q2", q2 or q1, model, ws.conn))
            return plan_inserts(model, pair, table_rules(model), scope)
    return build


def strategies(plan):
    return {f"{p.fk.table.name}.{p.fk.name}": p.strategy for p in plan.fks}


def test_parents_before_children(plan_for):
    plan = plan_for("""
    CREATE TABLE a (id int PRIMARY KEY);
    CREATE TABLE b (id int PRIMARY KEY, a_id int NOT NULL REFERENCES a);
    CREATE TABLE d (id int PRIMARY KEY, b_id int REFERENCES b, a_id int REFERENCES a);
    """, scope="all")
    order = plan.order
    assert order.index(q("a")) < order.index(q("b")) < order.index(q("d"))
    assert set(strategies(plan).values()) == {PARENT}


def test_only_tables_the_queries_need(plan_for):
    plan = plan_for("""
    CREATE TABLE country (code text PRIMARY KEY);
    CREATE TABLE users (id int PRIMARY KEY, country text REFERENCES country);
    CREATE TABLE orders (id int PRIMARY KEY, user_id int NOT NULL REFERENCES users);
    CREATE TABLE audit (id int, order_id int REFERENCES orders);
    CREATE TABLE unrelated (x int);
    """, "SELECT id FROM orders WHERE id > 1")
    assert plan.order == [q("country"), q("users"), q("orders")]       # FK parents pulled in, transitively
    assert plan.skipped[q("audit")] == NOT_NEEDED and plan.skipped[q("unrelated")] == NOT_NEEDED


def test_views_and_partitions_map_to_insert_targets(plan_for):
    plan = plan_for("""
    CREATE TABLE ev (d date NOT NULL, x int) PARTITION BY RANGE (d);
    CREATE TABLE ev_24 PARTITION OF ev FOR VALUES FROM ('2024-01-01') TO ('2025-01-01');
    CREATE TABLE t (id int);
    CREATE VIEW v AS SELECT * FROM t;
    """, "SELECT * FROM ev_24 JOIN v ON true")
    assert set(plan.order) == {q("ev"), q("t")}


def test_nullable_self_reference_null_then_update(plan_for):
    plan = plan_for("CREATE TABLE emp (id int PRIMARY KEY, boss int REFERENCES emp);", "SELECT * FROM emp")
    [p] = plan.fks
    assert p.strategy == NULL_THEN_UPDATE and p.null_columns == ("boss",) and plan.updates == (p,)


def test_not_null_self_reference_points_at_itself(plan_for):
    plan = plan_for("CREATE TABLE node (id int PRIMARY KEY, parent int NOT NULL REFERENCES node);",
                    "SELECT * FROM node")
    assert plan.fks[0].strategy == SELF and plan.order == [q("node")]


def test_self_reference_forbidden_by_check_is_skipped_and_cascades(plan_for):
    plan = plan_for("""
    CREATE TABLE node (id int PRIMARY KEY, parent int NOT NULL REFERENCES node, CHECK (parent <> id));
    CREATE TABLE leaf (id int, node_id int NOT NULL REFERENCES node);
    CREATE TABLE tag (id int, node_id int REFERENCES node);
    """, "SELECT * FROM leaf, tag")
    assert "CHECK forbids" in plan.skipped[q("node")]
    assert "needs a row in public.node" in plan.skipped[q("leaf")]
    assert strategies(plan) == {"tag.tag_node_id_fkey": NULL}           # nullable FK: left NULL
    assert plan.order == [q("tag")]


def test_cycle_broken_at_nullable_fk(plan_for):
    plan = plan_for("""
    CREATE TABLE dept (id int PRIMARY KEY, manager int);
    CREATE TABLE emp (id int PRIMARY KEY, dept_id int NOT NULL REFERENCES dept);
    ALTER TABLE dept ADD FOREIGN KEY (manager) REFERENCES emp;
    """, "SELECT * FROM dept JOIN emp ON emp.dept_id = dept.id")
    assert strategies(plan) == {"dept.dept_manager_fkey": NULL_THEN_UPDATE, "emp.emp_dept_id_fkey": PARENT}
    assert plan.order == [q("dept"), q("emp")] and not plan.make_deferrable


def test_not_null_cycle_is_deferred(plan_for):
    plan = plan_for("""
    CREATE TABLE a (id int PRIMARY KEY, b_id int NOT NULL);
    CREATE TABLE b (id int PRIMARY KEY, a_id int NOT NULL REFERENCES a DEFERRABLE);
    ALTER TABLE a ADD FOREIGN KEY (b_id) REFERENCES b;
    """, "SELECT * FROM a")
    [step] = plan.steps
    assert step.kind == "cycle" and set(step.tables) == {q("a"), q("b")}
    assert set(strategies(plan).values()) == {DEFERRED}
    assert [fk.name for fk in plan.make_deferrable] == ["a_b_id_fkey"]  # b's FK is already DEFERRABLE


def test_three_table_cycle_with_one_nullable_edge(plan_for):
    plan = plan_for("""
    CREATE TABLE a (id int PRIMARY KEY, c_id int);
    CREATE TABLE b (id int PRIMARY KEY, a_id int NOT NULL REFERENCES a);
    CREATE TABLE c (id int PRIMARY KEY, b_id int NOT NULL REFERENCES b);
    ALTER TABLE a ADD FOREIGN KEY (c_id) REFERENCES c;
    """, "SELECT * FROM c")
    assert plan.order == [q("a"), q("b"), q("c")]
    assert strategies(plan)["a.a_c_id_fkey"] == NULL_THEN_UPDATE


def test_composite_fks_order_like_single_ones(plan_for):
    plan = plan_for("""
    CREATE TABLE p (a int, b int, PRIMARY KEY (a, b));
    CREATE TABLE s (a int, b int NOT NULL, FOREIGN KEY (a, b) REFERENCES p MATCH SIMPLE);
    CREATE TABLE f (a int, b int, FOREIGN KEY (a, b) REFERENCES p MATCH FULL);
    """, "SELECT * FROM s, f")
    assert plan.order[0] == q("p") and all(p.strategy == PARENT for p in plan.fks)


def test_fk_into_table_without_rows(plan_for):
    plan = plan_for("""
    CREATE TABLE empty_parent (id int PRIMARY KEY) PARTITION BY LIST (id);
    CREATE TABLE s (a int, x int REFERENCES empty_parent);
    CREATE TABLE p (a int, b int, PRIMARY KEY (a, b));
    CREATE TABLE full_fk (a int, b int NOT NULL, FOREIGN KEY (a, b) REFERENCES p MATCH FULL);
    """, "SELECT * FROM s")
    assert "no partitions" in plan.skipped[q("empty_parent")]
    [p] = plan.fks
    assert p.strategy == NULL and p.null_columns == ("x",)


def test_match_full_null_needs_every_column_nullable(plan_for):
    plan = plan_for("""
    CREATE TABLE gone (a int, b int, PRIMARY KEY (a, b)) PARTITION BY LIST (a);
    CREATE TABLE simple_fk (a int, b int NOT NULL, FOREIGN KEY (a, b) REFERENCES gone MATCH SIMPLE);
    CREATE TABLE full_fk (a int, b int NOT NULL, FOREIGN KEY (a, b) REFERENCES gone MATCH FULL);
    """, "SELECT * FROM simple_fk, full_fk")
    assert plan.fks[0].fk.table == q("simple_fk") and plan.fks[0].null_columns == ("a",)   # one NULL is enough
    assert "needs a row in public.gone" in plan.skipped[q("full_fk")]                     # all or none


def test_unparsed_query_falls_back_to_all_tables(plan_for, monkeypatch):
    import cexgen.queries.analyzer as analyzer
    import sqlglot

    def boom(*a, **k):
        raise sqlglot.errors.ParseError("x")
    monkeypatch.setattr(analyzer, "ParsedQuery", boom)
    plan = plan_for("CREATE TABLE a (x int); CREATE TABLE b (y int);", "SELECT x FROM a")
    assert plan.scope == "all" and set(plan.order) == {q("a"), q("b")}
    assert "every table gets a row" in plan.warnings[0]


def test_trigger_warning(plan_for):
    plan = plan_for("""
    CREATE TABLE t (x int);
    CREATE FUNCTION f() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$;
    CREATE TRIGGER tg BEFORE INSERT ON t FOR EACH ROW EXECUTE FUNCTION f();
    """, "SELECT x FROM t")
    assert "fires trigger tg" in plan.warnings[0]


def test_plan_is_deterministic(plan_for):
    ddl = """
    CREATE TABLE z (id int PRIMARY KEY); CREATE TABLE y (id int PRIMARY KEY); CREATE TABLE x (id int PRIMARY KEY);
    CREATE TABLE w (z int REFERENCES z, y int REFERENCES y, x int REFERENCES x);
    """
    first = plan_for(ddl, scope="all").order
    assert first == [q("x"), q("y"), q("z"), q("w")]
    assert plan_for(ddl, scope="all").order == first
