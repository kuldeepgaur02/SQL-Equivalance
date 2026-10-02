"""Repair branches tested directly (the planner and solver usually prevent them end to end)."""
import psycopg2
import pytest

from cexgen.db import SchemaWorkspace
from cexgen.insertion import classify
from cexgen.insertion.fix_code import fix_foreign_key, fix_not_null, fix_unique
from cexgen.insertion.fix_rules import _fix_data, candidates, checks_pass
from cexgen.insertion.sql import array_literal, record_literal
from cexgen.schema import Family, QName, read_schema


def test_array_and_record_literals():
    assert array_literal([1, None, "a b", 'q"x'], Family.TEXT) == '{"1",NULL,"a b","q\\"x"}'
    assert array_literal([[1, 2], [3, 4]], Family.INTEGER) == '{{"1","2"},{"3","4"}}'
    assert array_literal([{"a": 1}], Family.JSON) == '{"{\\"a\\": 1}"}'
    assert record_literal(("a", None, True)) == '("a",,"t")'


def test_data_errors_by_rules():
    from cexgen.schema import Column, TypeInfo
    v3 = TypeInfo(QName("pg_catalog", "varchar"), "character varying(3)", "varchar", Family.TEXT, length=3)
    i2 = TypeInfo(QName("pg_catalog", "int2"), "smallint", "int2", Family.INTEGER, min_value=-32768, max_value=32767)

    class T:
        columns = {"s": Column("s", 1, v3, True), "n": Column("n", 2, i2, True)}
        insertable_columns = list(columns.values())
    assert _fix_data(T, {"s": "abcdef", "n": 1}, {"sqlstate": "22001"}) == {"s": "abc", "n": 1}
    assert _fix_data(T, {"s": "a", "n": 99999}, {"sqlstate": "22003"}) == {"s": "a", "n": 1}
    assert candidates(T.columns["s"], "x")[0] == "a" and None in candidates(T.columns["s"], "x")


def test_classify():
    def err(code, text=""):
        # a fresh error class per call: pgcode / pgerror are read-only on psycopg2.Error
        cls = type("E", (psycopg2.Error,), {"pgcode": property(lambda self: code),
                                            "pgerror": property(lambda self: text)})
        return cls(text)
    assert classify(err("23505")) == "unique"
    assert classify(err("23514", 'ERROR: no partition of relation "ev" found for row')) == "partition"
    assert classify(err("23514", "violates check constraint")) == "check"
    assert classify(err("22001")) == "data" and classify(err("P0001")) == "trigger"
    assert classify(err("XX000")) == "unknown"


@pytest.mark.db
def test_code_fixes_use_what_is_in_the_database(settings):
    ddl = """
    CREATE TABLE p (id int PRIMARY KEY, code text);
    INSERT INTO p VALUES (7, 'x');
    CREATE TABLE c (id int PRIMARY KEY, p_id int NOT NULL REFERENCES p, opt_id int REFERENCES p, note text NOT NULL,
                    UNIQUE (p_id));
    CREATE TABLE gone (id int PRIMARY KEY);
    CREATE TABLE c2 (g int REFERENCES gone, g2 int NOT NULL REFERENCES gone);
    """
    with SchemaWorkspace(ddl, settings) as ws:
        model = read_schema(ws.conn, ws.inventory)
        c, c2 = model.table("c"), model.table("c2")
        with ws.conn.cursor() as cur:
            row = {"id": 1, "p_id": 1, "opt_id": 1, "note": None}
            assert fix_foreign_key(cur, model, c, row, "c_p_id_fkey")["p_id"] == 7      # an existing parent (seed)
            assert fix_not_null(cur, model, c, row, "note")["note"] == "a"
            assert fix_not_null(cur, model, c, row, "p_id")["p_id"] == 7
            assert fix_unique(cur, c, row, "c_p_id_key", 1) is None                     # key made of FK columns only
            assert fix_unique(cur, model.table("p"), {"id": 7, "code": "x"}, "p_pkey", 1)["id"] == 8
            gone_row = {"g": 1, "g2": 1}
            assert fix_foreign_key(cur, model, c2, gone_row, "c2_g_fkey") == {"g": None, "g2": 1}  # nullable: NULL
            assert fix_foreign_key(cur, model, c2, gone_row, "c2_g2_fkey") is None                 # NOT NULL: no fix
            assert checks_pass(cur, c, row)                                                       # no CHECKs at all
        ws.conn.rollback()
