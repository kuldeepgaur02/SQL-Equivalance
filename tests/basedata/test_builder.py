"""Base data end to end: defaults, rules, partitions, FKs, and the JSON/ARRAY oracle."""
from decimal import Decimal

import pytest

from cexgen.basedata import build_base
from cexgen.basedata.model import DEFAULT, ENUM, FK, FK_NULL, JSON_RULE, LLM, LLM_MEMORY, RULE
from cexgen.db import SchemaWorkspace
from cexgen.errors import LLMUnavailable
from cexgen.journal import Journal
from cexgen.llm import LLMCallFailed, LLMResult, Oracle, register_provider
from cexgen.memory import SchemaMemory
from cexgen.ordering import plan_inserts
from cexgen.queries import QueryPair, analyze_query, table_rules
from cexgen.schema import QName, read_schema

pytestmark = pytest.mark.db


def q(name):
    return QName("public", name)


class FakeProvider:
    """Stands in for an LLM API: returns canned JSON, records what it was asked."""
    name = "fake"

    def __init__(self, answer=None, fail=False):
        self.answer, self.fail, self.calls = answer, fail, []

    def complete_json(self, system, prompt, schema, purpose):
        self.calls.append(prompt)
        if self.fail:
            raise LLMCallFailed("boom")
        return LLMResult(self.answer, "fake-model", {"input_tokens": 1, "output_tokens": 1})


@pytest.fixture
def base_for(settings, tmp_path):
    """base_for(ddl, q1, q2=None, provider=None, llm=True) -> (BaseData, journal entries, provider)"""
    def build(ddl, q1, q2=None, provider=None, llm=True, memory_dir=None):
        s = settings.with_(llm_enabled=llm, runs_dir=str(memory_dir or tmp_path))
        with SchemaWorkspace(ddl, s) as ws:
            model = read_schema(ws.conn, ws.inventory)
            pair = QueryPair(analyze_query("Q1", q1, model, ws.conn), analyze_query("Q2", q2 or q1, model, ws.conn))
            rules = table_rules(model)
            plan = plan_inserts(model, pair, rules)
            journal = Journal("t")
            memory = SchemaMemory.open(ws.fingerprint, ws.server_version, s)
            base = build_base(model, plan, rules, pair, Oracle(s, journal, memory, provider))
            memory.save()
            return base, journal.entries
    return build


def cells(base, table):
    return {c: (cell.value, cell.source) for c, cell in base.rows[q(table)].items()}


def test_defaults_enum_and_check_rules(base_for):
    base, _ = base_for("""
    CREATE TYPE mood AS ENUM ('sad', 'ok');
    CREATE TABLE t (
        id int PRIMARY KEY, name varchar(5), m mood NOT NULL, price numeric(6,2),
        age int CHECK (age >= 18), kind text CHECK (kind IN ('gold', 'silver')),
        code varchar(8) CHECK (code LIKE 'AC-%' AND length(code) = 6),
        lo int, hi int, CHECK (lo < hi), pct int CHECK (pct BETWEEN 5 AND 10) CHECK (pct <> 5)
    );
    """, "SELECT * FROM t")
    c = cells(base, "t")
    assert c["name"] == ("a", DEFAULT) and c["m"] == ("sad", ENUM) and c["price"] == (Decimal("1.00"), DEFAULT)
    assert c["age"] == (18, RULE) and c["kind"] == ("gold", RULE) and c["code"] == ("AC-aaa", RULE)
    assert c["lo"][0] < c["hi"][0]
    assert c["pct"] == (6, RULE)                    # BETWEEN 5 AND 10, and <> 5
    assert base.unsatisfied == ()


def test_queries_filters_are_ignored(base_for):
    base, _ = base_for("CREATE TABLE t (x int);", "SELECT * FROM t WHERE x > 100")
    assert cells(base, "t")["x"] == (1, DEFAULT)    # the diagram: scalars ignore the queries


def test_domain_checks_and_unsatisfiable_rules(base_for):
    base, _ = base_for("""
    CREATE DOMAIN positive AS int CHECK (VALUE > 0);
    CREATE TABLE t (p positive, odd int CHECK (odd % 2 = 1), bad int CHECK (bad > 5 AND bad < 3));
    """, "SELECT * FROM t")
    c = cells(base, "t")
    assert c["p"] == (1, DEFAULT)                   # already satisfies VALUE > 0
    text = " ".join(base.unsatisfied)
    assert "not understood" in text and "bad > 5" in text


def test_partition_bounds(base_for):
    base, _ = base_for("""
    CREATE TABLE ev (d date NOT NULL, kind text, v int) PARTITION BY RANGE (d);
    CREATE TABLE ev_old PARTITION OF ev FOR VALUES FROM (MINVALUE) TO ('1990-01-01');
    CREATE TABLE ev_24 PARTITION OF ev FOR VALUES FROM ('2024-01-01') TO ('2025-01-01') PARTITION BY LIST (kind);
    CREATE TABLE ev_24_b PARTITION OF ev_24 FOR VALUES IN ('b', 'c');
    CREATE TABLE n (id int NOT NULL) PARTITION BY RANGE (id);
    CREATE TABLE n1 PARTITION OF n FOR VALUES FROM (MINVALUE) TO (0);
    """, "SELECT * FROM ev, n")
    ev = cells(base, "ev")
    assert ev["d"][0] in ("1989-12-31", "2024-01-01")
    if ev["d"][0] == "2024-01-01":
        assert ev["kind"][0] == "b"
    assert cells(base, "n")["id"] == (-1, RULE)     # MINVALUE .. 0: just below the upper bound


def test_fk_strategies(base_for):
    base, _ = base_for("""
    CREATE TABLE dept (id int PRIMARY KEY, manager int);
    CREATE TABLE emp (id int PRIMARY KEY, dept_id int NOT NULL REFERENCES dept, boss int REFERENCES emp);
    ALTER TABLE dept ADD FOREIGN KEY (manager) REFERENCES emp;
    CREATE TABLE node (id int PRIMARY KEY, parent int NOT NULL REFERENCES node);
    """, "SELECT * FROM emp, node")
    assert cells(base, "emp")["dept_id"] == (1, FK)
    assert cells(base, "emp")["boss"] == (None, FK_NULL) and cells(base, "dept")["manager"] == (None, FK_NULL)
    assert cells(base, "node")["parent"] == (1, FK)
    assert {(str(u.table), u.fk.name, dict(u.values)["boss" if "boss" in u.values else "manager"]) for u in base.updates} \
        == {("public.dept", "dept_manager_fkey", 1), ("public.emp", "emp_boss_fkey", 1)}


def test_fk_copies_parent_key_after_rules(base_for):
    base, _ = base_for("""
    CREATE TABLE p (code text PRIMARY KEY CHECK (code LIKE 'P-%'));
    CREATE TABLE c (id int, p_code text NOT NULL REFERENCES p);
    """, "SELECT * FROM c")
    assert cells(base, "p")["code"] == ("P-", RULE) and cells(base, "c")["p_code"] == ("P-", FK)


JSON_DDL = "CREATE TABLE o (id int, meta jsonb, tags varchar(3)[] NOT NULL, moods text[]);"
JSON_Q = "SELECT id FROM o WHERE meta->>'channel' = 'web' AND (meta->'a'->>'b') IS NOT NULL"


def test_json_and_arrays_from_rules_in_no_llm_mode(base_for):
    base, entries = base_for(JSON_DDL, JSON_Q, llm=False)
    c = cells(base, "o")
    assert c["meta"] == ({"channel": "a", "a": {"b": "a"}}, JSON_RULE)
    assert c["tags"] == (["a"], JSON_RULE)
    assert not any(e.llm for e in entries)


def test_json_and_arrays_from_the_llm(base_for):
    fake = FakeProvider({"values": [
        {"table": "public.o", "column": "meta", "value_json": '{"channel": "web"}'},
        {"table": "public.o", "column": "tags", "value_json": '["x", "y"]'},
        {"table": "public.o", "column": "moods", "value_json": '["toolongforthree"]'},
    ]})
    base, entries = base_for(JSON_DDL, JSON_Q, provider=fake)
    c = cells(base, "o")
    assert c["meta"] == ({"channel": "web", "a": {"b": "a"}}, LLM)        # missing path added
    assert c["tags"] == (["x", "y"], LLM)
    assert c["moods"] == (["toolongforthree"], LLM)                       # text[] has no length limit
    assert len(fake.calls) == 1 and "channel" in fake.calls[0]            # one call for every column
    [call] = [e for e in entries if e.llm]
    assert call.status == "ok" and call.llm["provider"] == "fake"


def test_invalid_llm_answer_falls_back_per_column(base_for):
    fake = FakeProvider({"values": [
        {"table": "public.o", "column": "meta", "value_json": "not json"},
        {"table": "public.o", "column": "tags", "value_json": '["far too long"]'},
    ]})
    base, _ = base_for(JSON_DDL, JSON_Q, provider=fake)
    meta, tags = base.rows[q("o")]["meta"], base.rows[q("o")]["tags"]
    assert meta.source == JSON_RULE and "not valid JSON" in meta.note
    assert tags.source == JSON_RULE and "character varying(3)" in tags.note


def test_failed_llm_call_falls_back_and_is_journaled(base_for):
    base, entries = base_for(JSON_DDL, JSON_Q, provider=FakeProvider(fail=True))
    assert base.rows[q("o")]["meta"].source == JSON_RULE
    [call] = [e for e in entries if e.llm]
    assert call.status == "failed" and call.detail["fallback"] == "rule table"


def test_llm_answers_are_reused_from_schema_memory(base_for, tmp_path):
    answer = {"values": [{"table": "public.o", "column": c, "value_json": v}
                         for c, v in (("meta", '{"channel": "web"}'), ("tags", '["x"]'), ("moods", '["m"]'))]}
    first = FakeProvider(answer)
    base_for(JSON_DDL, JSON_Q, provider=first, memory_dir=tmp_path)
    second = FakeProvider(answer)
    base, _ = base_for(JSON_DDL, JSON_Q, provider=second, memory_dir=tmp_path)
    assert second.calls == [] and base.rows[q("o")]["meta"].source == LLM_MEMORY


def test_no_llm_needed_means_no_credentials_needed(base_for):
    class Exploding:
        name = "exploding"

        def complete_json(self, *a):
            raise LLMUnavailable("should not be called")
    base, _ = base_for("CREATE TABLE t (x int);", "SELECT x FROM t", provider=Exploding())
    assert cells(base, "t")["x"] == (1, DEFAULT)


def test_missing_credentials_stop_the_case(base_for):
    class NoKey:
        name = "nokey"

        def complete_json(self, *a):
            raise LLMUnavailable("no credentials")
    with pytest.raises(LLMUnavailable):
        base_for(JSON_DDL, JSON_Q, provider=NoKey())


def test_a_new_provider_plugs_in_through_the_registry(settings):
    from cexgen.llm import get_provider
    register_provider("fake", lambda s: FakeProvider({"values": []}))
    assert get_provider(settings.with_(llm_provider="fake")).name == "fake"
    with pytest.raises(LLMUnavailable, match="unknown LLM provider"):
        get_provider(settings.with_(llm_provider="nope"))
