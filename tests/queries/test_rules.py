"""Step 2's rules broken down by the same analyser."""
import pytest

from cexgen.db import SchemaWorkspace
from cexgen.queries import table_rules
from cexgen.queries.describe import describe_predicate
from cexgen.schema import QName, read_schema

pytestmark = pytest.mark.db


def rules_of(settings, ddl):
    with SchemaWorkspace(ddl, settings) as ws:
        model = read_schema(ws.conn, ws.inventory)
    return {r.name: r for rs in table_rules(model).values() for r in rs}


def test_check_rules(settings):
    rules = rules_of(settings, """
    CREATE DOMAIN positive AS int CHECK (VALUE > 0);
    CREATE TABLE t (
        age int CHECK (age >= 18),
        code varchar(8) CONSTRAINT code_shape CHECK (code LIKE 'AC-%' AND length(code) = 6),
        kind text CONSTRAINT kind_values CHECK (kind IN ('gold', 'silver')),
        lo int, hi int, CONSTRAINT lo_hi CHECK (lo < hi),
        a int, b int, CONSTRAINT either CHECK (a > 0 OR b > 0),
        n positive,
        "Weird" int CONSTRAINT weird CHECK ("Weird" <> 0),
        f text CONSTRAINT fn CHECK (my_func(f))
    );
    """.replace("my_func(f)", "f ~ '^[a-z]+$' AND length(f) < 10"))
    assert rules["t_age_check"].understood
    assert [describe_predicate(p) for p in rules["t_age_check"].predicates] == ["public.t.age >= 18"]
    assert rules["code_shape"].understood and len(rules["code_shape"].predicates) == 2
    assert rules["code_shape"].predicates[1].term.function == "length"
    assert rules["kind_values"].predicates[0].op == "in"            # Postgres stores it as = ANY (ARRAY[...])
    assert rules["lo_hi"].understood and rules["lo_hi"].comparisons[0].op == "<"
    assert not rules["either"].understood                          # OR: no single value rule
    assert rules["positive_check"].kind == "domain_check" and rules["positive_check"].understood
    assert rules["weird"].predicates[0].term.column.column == "Weird"
    assert rules["fn"].understood


def test_index_and_exclusion_predicates(settings):
    rules = rules_of(settings, """
    CREATE EXTENSION IF NOT EXISTS btree_gist;
    CREATE TABLE t (email text, deleted_at timestamp, room int, during tsrange, cancelled bool);
    CREATE UNIQUE INDEX live_email ON t (email) WHERE deleted_at IS NULL;
    ALTER TABLE t ADD CONSTRAINT no_overlap EXCLUDE USING gist (room WITH =, during WITH &&) WHERE (NOT cancelled);
    """)
    assert rules["live_email"].kind == "unique_predicate"
    assert rules["live_email"].predicates[0].op == "is_null"
    p = rules["no_overlap"].predicates[0]
    assert (p.op, p.negated, p.term.column.table) == ("is_true", True, QName("public", "t"))


def test_unparseable_rule_is_left_to_repair(settings):
    rules = rules_of(settings, "CREATE TABLE t (x int CONSTRAINT odd CHECK (x % 2 = 1));")
    assert not rules["odd"].understood and rules["odd"].unparsed
