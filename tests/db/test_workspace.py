import pytest

from cexgen.db import SchemaWorkspace, schema_fingerprint
from cexgen.errors import SchemaError

pytestmark = pytest.mark.db

SEEDED = r"""
CREATE TABLE countries (code char(2) PRIMARY KEY, name text, rate numeric(6,3), tags text[], info jsonb, raw bytea);
INSERT INTO countries VALUES ('IN', 'India', 1.500, '{a,"b c"}', '{"x": [1, 2]}', '\x00ff'),
                             ('US', E'tab\there', NULL, NULL, NULL, NULL);
CREATE TABLE users (
    id      int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    country char(2) NOT NULL REFERENCES countries,
    shout   text GENERATED ALWAYS AS (upper(country)) STORED
);
INSERT INTO users (country) VALUES ('IN');
CREATE SEQUENCE ticket START 100;
SELECT nextval('ticket');
CREATE SEQUENCE unused START 7;
"""


def rows(ws, sql):
    with ws.conn.cursor() as cur:
        cur.execute(sql)
        out = cur.fetchall()
    ws.conn.rollback()
    return out


def dirty(ws, sql):
    with ws.conn.cursor() as cur:
        cur.execute(sql)
    ws.conn.commit()


def test_reset_restores_seed_rows_exactly(settings):
    with SchemaWorkspace(SEEDED, settings) as ws:
        before = rows(ws, "SELECT * FROM countries ORDER BY code")
        dirty(ws, "DELETE FROM users; DELETE FROM countries; INSERT INTO countries (code) VALUES ('FR')")
        ws.reset()
        assert rows(ws, "SELECT * FROM countries ORDER BY code") == before
        assert rows(ws, "SELECT id, country, shout FROM users") == [(1, "IN", "IN")]


def test_reset_restores_sequence_counters(settings):
    with SchemaWorkspace(SEEDED, settings) as ws:
        dirty(ws, "SELECT nextval('ticket'); SELECT nextval('unused'); INSERT INTO users (country) VALUES ('US'), ('US')")
        ws.reset()
        assert rows(ws, "SELECT nextval('ticket')") == [(101,)]       # schema had called it once (100)
        assert rows(ws, "SELECT nextval('unused')") == [(7,)]         # never called: starts at START
        assert rows(ws, "INSERT INTO users (country) VALUES ('US') RETURNING id") == [(2,)]


def test_reset_does_not_fire_triggers(settings):
    ddl = """
    CREATE TABLE t (x int);
    INSERT INTO t VALUES (1);
    CREATE FUNCTION boom() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'fired'; END $$;
    CREATE TRIGGER no_writes BEFORE INSERT OR DELETE OR TRUNCATE ON t FOR EACH STATEMENT EXECUTE FUNCTION boom();
    """
    with SchemaWorkspace(ddl, settings) as ws:
        ws.reset()
        assert rows(ws, "SELECT x FROM t") == [(1,)]


def test_reset_empties_unseeded_tables_and_cascades(settings):
    ddl = """
    CREATE TABLE parent (id int PRIMARY KEY);
    CREATE TABLE child (id int PRIMARY KEY, parent_id int REFERENCES parent);
    """
    with SchemaWorkspace(ddl, settings) as ws:
        dirty(ws, "INSERT INTO parent VALUES (1); INSERT INTO child VALUES (1, 1)")
        ws.reset()
        assert rows(ws, "SELECT (SELECT count(*) FROM parent), (SELECT count(*) FROM child)") == [(0, 0)]


def test_partitioned_and_inherited_tables_reset(settings):
    ddl = """
    CREATE TABLE events (d date NOT NULL, v int) PARTITION BY RANGE (d);
    CREATE TABLE events_2024 PARTITION OF events FOR VALUES FROM ('2024-01-01') TO ('2025-01-01');
    INSERT INTO events VALUES ('2024-05-01', 1);
    CREATE TABLE animal (name text);
    CREATE TABLE dog (barks bool) INHERITS (animal);
    INSERT INTO dog VALUES ('rex', true);
    """
    with SchemaWorkspace(ddl, settings) as ws:
        dirty(ws, "INSERT INTO events VALUES ('2024-06-01', 2); INSERT INTO dog VALUES ('fido', false)")
        ws.reset()
        assert rows(ws, "SELECT v FROM events") == [(1,)]
        assert rows(ws, "SELECT name FROM animal") == [("rex",)]


def test_materialized_views_refreshed_in_dependency_order(settings):
    ddl = """
    CREATE TABLE t (x int);
    INSERT INTO t VALUES (1), (2);
    CREATE MATERIALIZED VIEW total AS SELECT sum(x) AS s FROM t;
    CREATE MATERIALIZED VIEW doubled AS SELECT s * 2 AS d FROM total;
    CREATE MATERIALIZED VIEW empty_until_refresh AS SELECT x FROM t WITH NO DATA;
    """
    with SchemaWorkspace(ddl, settings) as ws:
        dirty(ws, "INSERT INTO t VALUES (100)")
        ws.refresh_materialized_views()
        assert rows(ws, "SELECT d FROM doubled") == [(206,)]
        ws.reset()
        assert rows(ws, "SELECT d FROM doubled") == [(6,)]
        assert rows(ws, "SELECT count(*) FROM empty_until_refresh") == [(2,)]


def test_bad_schema_drops_the_database(settings):
    with pytest.raises(SchemaError):
        SchemaWorkspace("CREATE TABLE t (x nosuchtype);", settings)


def test_fingerprint_ignores_formatting_not_content():
    a = "CREATE TABLE t (x int); -- comment"
    assert schema_fingerprint(a) == schema_fingerprint("create   table T (X INT);")
    assert schema_fingerprint(a) != schema_fingerprint("CREATE TABLE t (x bigint);")
    assert schema_fingerprint("CREATE TABLE t (x text DEFAULT 'A')") != schema_fingerprint("CREATE TABLE t (x text DEFAULT 'a')")
