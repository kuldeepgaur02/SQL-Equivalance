"""General integrity: CHECK, EXCLUDE, partitioning; plus physical facts and views."""
import pytest

from cexgen.schema import QName

pytestmark = pytest.mark.db


def test_checks_single_and_multi_column(model_of):
    m = model_of("""
    CREATE TABLE t (lo int, hi int, s text,
        CHECK (lo < hi),
        CONSTRAINT s_shape CHECK (s LIKE 'A-%' AND length(s) = 5) NO INHERIT);
    """)
    checks = {c.name: c for c in m.table("t").checks}
    multi = next(c for n, c in checks.items() if n != "s_shape")
    assert multi.columns == ("lo", "hi") and multi.expression == "lo < hi"
    assert checks["s_shape"].no_inherit and checks["s_shape"].columns == ("s",)
    assert "length(s) = 5" in checks["s_shape"].expression


def test_strip_check_keeps_inner_parentheses():
    from cexgen.schema.reader import strip_check
    assert strip_check("CHECK ((a > 0) AND (b > 0))") == "(a > 0) AND (b > 0)"
    assert strip_check("CHECK ((x = ')')) NOT VALID") == "x = ')'"
    assert strip_check("CHECK (a > 0) NO INHERIT NOT VALID") == "a > 0"


def test_exclusion_constraint(model_of):
    m = model_of("""
    CREATE EXTENSION IF NOT EXISTS btree_gist;
    CREATE TABLE booking (room int, during tsrange, cancelled bool,
        EXCLUDE USING gist (room WITH =, during WITH &&) WHERE (NOT cancelled));
    """)
    [ex] = m.table("booking").exclusions
    assert ex.method == "gist"
    assert ex.elements == (("room", "="), ("during", "&&"))
    assert ex.predicate == "NOT cancelled"


def test_range_list_hash_default_and_nested_partitions(model_of):
    m = model_of("""
    CREATE TABLE ev (d date NOT NULL, kind text, id int) PARTITION BY RANGE (d);
    CREATE TABLE ev_old PARTITION OF ev FOR VALUES FROM (MINVALUE) TO ('2024-01-01');
    CREATE TABLE ev_24 PARTITION OF ev FOR VALUES FROM ('2024-01-01') TO ('2025-01-01') PARTITION BY LIST (kind);
    CREATE TABLE ev_24_a PARTITION OF ev_24 FOR VALUES IN ('a', NULL);
    CREATE TABLE ev_24_rest PARTITION OF ev_24 DEFAULT;
    CREATE TABLE h (id int) PARTITION BY HASH (id);
    CREATE TABLE h0 PARTITION OF h FOR VALUES WITH (modulus 2, remainder 0);
    CREATE TABLE h1 PARTITION OF h FOR VALUES WITH (modulus 2, remainder 1);
    CREATE TABLE expr_key (d timestamp) PARTITION BY RANGE (date_trunc('month', d));
    CREATE TABLE empty_parent (x int) PARTITION BY LIST (x);
    """)
    ev = m.table("ev")
    assert ev.kind == "partitioned table" and ev.insert_target
    assert ev.partitioning.strategy == "range" and ev.partitioning.key == ("d",)
    assert not ev.partitioning.has_default
    bounds = {str(p.table): p.bound for p in ev.partitioning.partitions}
    assert bounds["public.ev_old"].lower[0].kind == "minvalue"
    sub = m.table("ev_24")
    assert sub.kind == "partition" and not sub.insert_target and sub.partition_of == QName("public", "ev")
    assert sub.partitioning.strategy == "list" and sub.partitioning.has_default
    assert {p.bound.kind for p in m.table("h").partitioning.partitions} == {"hash"}
    assert m.table("expr_key").partitioning.key == (None,)
    assert "date_trunc" in m.table("expr_key").partitioning.key_sql
    assert any("empty_parent is partitioned but has no partitions" in w for w in m.warnings)
    assert {t.qname.name for t in m.insert_targets} == {"ev", "h", "expr_key", "empty_parent"}


def test_fk_to_partitioned_table(model_of):
    m = model_of("""
    CREATE TABLE p (id int, d date, PRIMARY KEY (id, d)) PARTITION BY RANGE (d);
    CREATE TABLE p1 PARTITION OF p FOR VALUES FROM ('2024-01-01') TO ('2025-01-01');
    CREATE TABLE c (pid int, pd date, FOREIGN KEY (pid, pd) REFERENCES p);
    """)
    assert m.table("c").foreign_keys[0].ref_table == QName("public", "p")


def test_inheritance(model_of):
    m = model_of("""
    CREATE TABLE animal (name text NOT NULL, CHECK (name <> ''));
    CREATE TABLE dog (barks bool) INHERITS (animal);
    """)
    dog, animal = m.table("dog"), m.table("animal")
    assert dog.inherits == (QName("public", "animal"),) and animal.inherited_by == (QName("public", "dog"),)
    assert dog.columns["name"].inherited and not dog.columns["barks"].inherited
    assert dog.checks and dog.insert_target and animal.insert_target
    assert any("also returns rows of its inheriting tables" in w for w in m.warnings)


def test_views_and_matviews_record_what_they_read(model_of):
    m = model_of("""
    CREATE TABLE a (x int); CREATE TABLE b (y int);
    CREATE VIEW v AS SELECT x, y FROM a JOIN b ON a.x = b.y;
    CREATE MATERIALIZED VIEW mv AS SELECT count(*) AS n FROM v;
    """)
    assert m.views[QName("public", "v")].reads == (QName("public", "a"), QName("public", "b"))
    assert m.views[QName("public", "mv")].reads == (QName("public", "v"),)
    assert m.resolve("v") == QName("public", "v")
    assert list(m.views[QName("public", "v")].columns) == ["x", "y"]


def test_triggers_rules_rls_seed_rows(model_of):
    m = model_of("""
    CREATE TABLE t (x int);
    INSERT INTO t VALUES (1), (2);
    CREATE FUNCTION f() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$;
    CREATE TRIGGER before_ins BEFORE INSERT OR UPDATE ON t FOR EACH ROW EXECUTE FUNCTION f();
    CREATE TRIGGER after_trunc AFTER TRUNCATE ON t FOR EACH STATEMENT EXECUTE FUNCTION f();
    ALTER TABLE t DISABLE TRIGGER after_trunc;
    CREATE RULE r AS ON DELETE TO t DO INSTEAD NOTHING;
    ALTER TABLE t ENABLE ROW LEVEL SECURITY;
    ALTER TABLE t FORCE ROW LEVEL SECURITY;
    """)
    t = m.table("t")
    trg = {x.name: x for x in t.triggers}
    assert (trg["before_ins"].timing, trg["before_ins"].events, trg["before_ins"].level) == ("before", ("insert", "update"), "row")
    assert trg["after_trunc"].events == ("truncate",) and not trg["after_trunc"].enabled
    assert t.rules == ("r",) and t.row_security and t.force_row_security
    assert t.seed_rows == 2


def test_foreign_table_is_not_an_insert_target(model_of):
    m = model_of("""
    CREATE EXTENSION IF NOT EXISTS postgres_fdw;
    CREATE SERVER nowhere FOREIGN DATA WRAPPER postgres_fdw;
    CREATE FOREIGN TABLE remote (x int) SERVER nowhere;
    """)
    t = m.table("remote")
    assert t.kind == "foreign table" and not t.insert_target
    assert any("foreign table" in w for w in m.warnings)


def test_zero_column_table(model_of):
    m = model_of("CREATE TABLE nothing ();")
    assert m.table("nothing").columns == {} and m.table("nothing").insert_target
