from datetime import timedelta

import psycopg2
import pytest

from cexgen.config import Settings
from cexgen.db import Sandbox, cleanup_orphans
from cexgen.db.connection import connect, safe_dsn
from cexgen.errors import DatabaseUnavailable, SchemaError

pytestmark = pytest.mark.db


def db_exists(settings: Settings, name: str) -> bool:
    conn = connect(settings, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
            return cur.fetchone() is not None
    finally:
        conn.close()


def test_database_created_and_dropped(settings):
    with Sandbox(settings) as box:
        assert db_exists(settings, box.name)
        inv = box.apply_ddl("CREATE TABLE t (x int);")
        assert [r.qualified for r in inv.tables] == ["public.t"]
    assert not db_exists(settings, box.name)


def test_dropped_even_when_the_block_raises(settings):
    with pytest.raises(RuntimeError):
        with Sandbox(settings) as box:
            raise RuntimeError("boom")
    assert not db_exists(settings, box.name)


def test_keep_db(settings):
    box = Sandbox(settings.with_(keep_db=True))
    box.close()
    try:
        assert db_exists(settings, box.name)
    finally:
        cleanup_orphans(settings, older_than=timedelta(seconds=-60))


def test_close_twice_is_safe(settings):
    box = Sandbox(settings)
    box.close()
    box.close()


def test_two_sandboxes_are_isolated(settings):
    with Sandbox(settings) as a, Sandbox(settings) as b:
        a.apply_ddl("CREATE TABLE public.t (x int);")
        b.apply_ddl("CREATE TABLE public.t (y text);")    # same qualified name: no clash
        assert a.name != b.name


def test_schema_sql_is_atomic(settings):
    with Sandbox(settings) as box:
        with pytest.raises(SchemaError):
            box.apply_ddl("CREATE TABLE a (x int);\nCREATE TABLE b (x nosuchtype);")
        assert box.inventory().tables == []                  # table a was rolled back too


def test_syntax_error_points_at_line_and_column(settings):
    ddl = "CREATE TABLE a (x int);\nCREATE TABEL b (x int);"
    with Sandbox(settings) as box, pytest.raises(SchemaError) as e:
        box.apply_ddl(ddl)
    assert e.value.sqlstate == "42601" and e.value.line == 2 and e.value.column == 8
    assert "CREATE TABEL" in e.value.excerpt


def test_missing_role_gets_pg_dump_hint(settings):
    ddl = "CREATE TABLE a (x int);\nALTER TABLE a OWNER TO someone_that_does_not_exist;"
    with Sandbox(settings) as box, pytest.raises(SchemaError, match="--no-owner"):
        box.apply_ddl(ddl)


def test_cluster_wide_statements_refused_before_running(settings):
    with Sandbox(settings) as box:
        for ddl in ("CREATE ROLE x;", "create   user y;", "ALTER SYSTEM SET work_mem = '1MB';",
                    "CREATE TABLE t (x int);\nDROP DATABASE postgres;"):
            with pytest.raises(SchemaError, match="whole Postgres server"):
                box.apply_ddl(ddl)
        assert box.inventory().tables == []


def test_session_changes_are_reset(settings):
    with Sandbox(settings) as box:
        box.apply_ddl("SET search_path = ''; SET statement_timeout = 1; CREATE TABLE public.t (x int);")
        with box.conn.cursor() as cur:
            cur.execute("SHOW search_path")
            assert cur.fetchone()[0] == '"$user", public'
            cur.execute("SHOW statement_timeout")
            assert cur.fetchone()[0] == "5s"
        box.conn.rollback()


def test_pinned_session_settings(settings):
    with Sandbox(settings) as box, box.conn.cursor() as cur:
        cur.execute("SHOW TimeZone")
        assert cur.fetchone()[0] == "UTC"
        cur.execute("SELECT datcollate FROM pg_database WHERE datname = current_database()")
        assert cur.fetchone()[0] == "C"


def test_pg_dump_style_schema(settings):
    ddl = """
    SET statement_timeout = 0;
    SET client_encoding = 'UTF8';
    SELECT pg_catalog.set_config('search_path', '', false);
    CREATE SCHEMA app;
    CREATE TYPE app.mood AS ENUM ('sad', 'ok');
    CREATE TABLE app.people (id integer NOT NULL, mood app.mood);
    ALTER TABLE ONLY app.people ADD CONSTRAINT people_pkey PRIMARY KEY (id);
    """
    with Sandbox(settings) as box:
        inv = box.apply_ddl(ddl)
    assert [r.qualified for r in inv.tables] == ["app.people"]
    assert inv.enums == ["app.mood"]


def test_inventory_warnings(settings):
    ddl = """
    CREATE TABLE lookup (code text PRIMARY KEY);
    INSERT INTO lookup VALUES ('a'), ('b');
    CREATE TABLE log (x int);
    CREATE FUNCTION f() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$;
    CREATE TRIGGER t1 AFTER INSERT ON lookup FOR EACH ROW EXECUTE FUNCTION f();
    CREATE RULE r1 AS ON INSERT TO log DO INSTEAD NOTHING;
    CREATE TABLE secret (x int);
    ALTER TABLE secret ENABLE ROW LEVEL SECURITY;
    ALTER TABLE secret FORCE ROW LEVEL SECURITY;
    CREATE VIEW v AS SELECT * FROM log;
    CREATE TABLE parts (d date) PARTITION BY RANGE (d);
    CREATE TABLE parts_2024 PARTITION OF parts FOR VALUES FROM ('2024-01-01') TO ('2025-01-01');
    CREATE TEMP TABLE scratch (x int);
    """
    with Sandbox(settings) as box:
        inv = box.apply_ddl(ddl)
    text = "\n".join(inv.warnings())
    assert "inserted 2 row(s) into public.lookup" in text
    assert "trigger t1 on public.lookup" in text
    assert "rule r1 on public.log" in text
    assert "forces row-level security" in text
    assert "temporary relation" in text
    assert {r.qualified for r in inv.tables} == {"public.lookup", "public.log", "public.secret", "public.parts"}
    assert [r.qualified for r in inv.of_kind("view")] == ["public.v"]


def test_extension_objects_are_not_listed_as_user_objects(settings):
    with Sandbox(settings) as box:
        inv = box.apply_ddl("CREATE EXTENSION IF NOT EXISTS citext; CREATE TABLE t (e citext);")
    assert inv.functions == 0 and any(e.startswith("citext") for e in inv.extensions)


def test_percent_signs_in_ddl_are_not_parameters(settings):
    with Sandbox(settings) as box:
        box.apply_ddl("CREATE TABLE t (x text CHECK (x LIKE '%a%'));")


def test_cleanup_only_touches_our_names(settings):
    admin = connect(settings, autocommit=True)
    try:
        with admin.cursor() as cur:
            cur.execute("DROP DATABASE IF EXISTS cex_not_a_sandbox")
            cur.execute("CREATE DATABASE cex_not_a_sandbox")
        stale = Sandbox(settings.with_(keep_db=True))
        stale.close()
        dropped = cleanup_orphans(settings, older_than=timedelta(seconds=-60))
        assert stale.name in dropped and "cex_not_a_sandbox" not in dropped
        assert db_exists(settings, "cex_not_a_sandbox")
    finally:
        with admin.cursor() as cur:
            cur.execute("DROP DATABASE IF EXISTS cex_not_a_sandbox")
        admin.close()


def test_unreachable_server_message():
    s = Settings(dsn="postgresql://cex:secret@localhost:1/cex", connect_timeout_s=1)
    with pytest.raises(DatabaseUnavailable, match="docker compose up -d") as e:
        Sandbox(s)
    assert "secret" not in str(e.value)


def test_safe_dsn_hides_password():
    assert "secret" not in safe_dsn("postgresql://u:secret@h:5/d")
