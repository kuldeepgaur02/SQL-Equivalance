"""Entity and referential integrity: primary keys, unique keys and indexes, foreign keys."""
import pytest

from cexgen.schema import QName

pytestmark = pytest.mark.db


def test_composite_pk_and_unique_variants(model_of):
    m = model_of("""
    CREATE TABLE t (a int, b int, email text, code text, note text, PRIMARY KEY (a, b), UNIQUE (code));
    CREATE UNIQUE INDEX t_email_lower ON t (lower(email));
    CREATE UNIQUE INDEX t_note_partial ON t (note) WHERE a > 0;
    CREATE UNIQUE INDEX t_code_incl ON t (code) INCLUDE (note);
    CREATE INDEX t_plain ON t (note);
    """)
    t = m.table("t")
    assert t.primary_key.columns == ("a", "b")
    by_name = {u.name: u for u in t.unique_keys}
    assert set(by_name) == {"t_code_key", "t_email_lower", "t_note_partial", "t_code_incl"}
    assert by_name["t_code_key"].is_constraint and not by_name["t_email_lower"].is_constraint
    assert by_name["t_email_lower"].elements == ("lower(email)",) and by_name["t_email_lower"].columns == ()
    assert by_name["t_note_partial"].predicate == "a > 0"
    assert by_name["t_code_incl"].elements == ("code",)          # INCLUDE columns are not part of the key


def test_nulls_not_distinct(model_of):
    from cexgen.errors import SchemaError
    try:
        m = model_of("CREATE TABLE t (x int, UNIQUE NULLS NOT DISTINCT (x));")
    except SchemaError:
        pytest.skip("NULLS NOT DISTINCT needs PostgreSQL 15")
    assert m.table("t").unique_keys[0].nulls_not_distinct


def test_foreign_key_details(model_of):
    m = model_of("""
    CREATE SCHEMA app;
    CREATE TABLE app.parent (a int, b int, code text UNIQUE, PRIMARY KEY (a, b));
    CREATE TABLE child (
        id int PRIMARY KEY,
        pa int, pb int,
        pcode text REFERENCES app.parent (code) ON DELETE SET NULL ON UPDATE CASCADE,
        boss int REFERENCES child (id) DEFERRABLE INITIALLY DEFERRED,
        FOREIGN KEY (pa, pb) REFERENCES app.parent MATCH FULL
    );
    """)
    fks = {fk.ref_columns: fk for fk in m.table("child").foreign_keys}
    composite = fks[("a", "b")]
    assert composite.ref_table == QName("app", "parent") and composite.match == "full"
    by_code = fks[("code",)]                                    # FK to a UNIQUE column, not the PK
    assert (by_code.on_delete, by_code.on_update) == ("set null", "cascade")
    boss = fks[("id",)]
    assert boss.self_reference and boss.deferrable and boss.deferred
    assert [fk.table for fk in m.references_to(QName("app", "parent"))] == [QName("public", "child")] * 2


def test_fk_can_be_null_depends_on_match(model_of):
    m = model_of("""
    CREATE TABLE p (a int, b int, PRIMARY KEY (a, b));
    CREATE TABLE s (a int NOT NULL, b int, FOREIGN KEY (a, b) REFERENCES p MATCH SIMPLE);
    CREATE TABLE f (a int NOT NULL, b int, FOREIGN KEY (a, b) REFERENCES p MATCH FULL);
    """)
    s, f = m.table("s"), m.table("f")
    assert s.fk_can_be_null(s.foreign_keys[0])                  # b can be NULL: the FK is skipped
    assert not f.fk_can_be_null(f.foreign_keys[0])              # FULL: all or none, and a is NOT NULL


def test_not_valid_fk_and_check(model_of):
    m = model_of("""
    CREATE TABLE p (id int PRIMARY KEY);
    CREATE TABLE c (pid int, x int);
    ALTER TABLE c ADD CONSTRAINT c_pid FOREIGN KEY (pid) REFERENCES p NOT VALID;
    ALTER TABLE c ADD CONSTRAINT c_x CHECK (x > 0) NOT VALID;
    """)
    c = m.table("c")
    assert not c.foreign_keys[0].validated and not c.checks[0].validated
    assert c.checks[0].expression == "x > 0"
    assert sum("NOT VALID" in w for w in m.warnings) == 2


def test_case_sensitive_and_reserved_names(model_of):
    m = model_of("""
    CREATE TABLE "User" ("Id" int PRIMARY KEY, "select" text);
    CREATE TABLE "user" (id int REFERENCES "User" ("Id"));
    """)
    assert m.table('"User"').columns["select"]
    lower = m.table('"user"')
    assert lower.foreign_keys[0].ref_table == QName("public", "User")
    assert str(lower.qname) == 'public."user"'
    assert m.table("User").qname == QName("public", "user")      # unquoted names fold to lower case
    with pytest.raises(KeyError):
        m.table('"USER"')


def test_same_name_in_two_schemas_and_search_path(model_of):
    m = model_of("CREATE SCHEMA app; CREATE TABLE app.t (a int); CREATE TABLE public.t (b int);")
    assert m.resolve("t") == QName("public", "t")
    assert list(m.table("app.t").columns) == ["a"]
    assert m.resolve("missing") is None and m.resolve("nope.t") is None
