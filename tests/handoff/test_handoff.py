"""Step 9: stop on a counterexample, or hand off to the mutation stage, with a verified replay."""
import json

import pytest

from cexgen.compare import compare, run_pair
from cexgen.db import SchemaWorkspace
from cexgen.handoff import (COLUMN_ORDER_ONLY, COUNTEREXAMPLE, FORMAT, MUTATION_ACTIONS, TO_MUTATION, UNTRUSTED,
                            verify)
from cexgen.input import Case
from cexgen.runner import run_cases

pytestmark = pytest.mark.db


@pytest.fixture
def run(settings, tmp_path):
    def go(ddl, q1, q2):
        s = settings.with_(llm_enabled=False, runs_dir=str(tmp_path))
        [srun] = run_cases([Case("c", ddl, q1, q2)], s)
        [c] = srun.cases
        assert c.status == "ok", c.error
        path = c.state["handoff_path"]
        return c.state["handoff"], json.loads(path.read_text()), c
    return go


T = "CREATE TABLE t (id int PRIMARY KEY, x int CHECK (x > 0));"


def test_counterexample_stops_with_a_verified_replay(run):
    doc, on_disk, _ = run(T, "SELECT x FROM t", "SELECT x FROM t WHERE x > 5")
    assert doc == on_disk and doc["format"] == FORMAT
    assert doc["status"] == COUNTEREXAMPLE and doc["starting_point"] is None
    assert doc["result"]["outcome"] == "differ" and doc["result"]["q1_rows"] == [[1]]
    assert doc["data"]["replay_verified"] is True


def test_same_is_handed_to_mutation_with_everything_it_needs(run):
    doc, _, c = run(T, "SELECT x FROM t WHERE x > 5", "SELECT x FROM t WHERE x >= 6")
    assert doc["status"] == TO_MUTATION and doc["starting_point"] == "same"
    assert doc["schema"]["sql"] == T and doc["schema"]["tables"]["public.t"]["checks"] == ["x > 0"]
    assert doc["queries"]["q1"] == "SELECT x FROM t WHERE x > 5"
    assert doc["queries"]["q1_analysis"]["filters"] == ["public.t.x > 5"]
    assert doc["data"]["tables"]["public.t"]["rows"] == [[1, 6]]
    assert doc["data"]["base_row_sources"]["public.t"]["x"] == "rebuild-rule"
    assert [(a["round"], a["comparison"]["outcome"]) for a in doc["attempts"]] == [(0, "both_empty"), (1, "same")]
    assert doc["mutation"]["actions"] == MUTATION_ACTIONS
    assert doc["journal"] == str(c.journal_path) and doc["data"]["replay_verified"] is True


def test_both_empty_after_rebuilds_is_flagged(run):
    doc, _, _ = run(T, "SELECT x FROM t WHERE x > 1 AND x < 1", "SELECT x FROM t WHERE x = 0")
    assert doc["status"] == TO_MUTATION and doc["starting_point"] == "both_empty"
    assert doc["flags"]["both_empty_after_rebuilds"] is True


def test_nondeterministic_difference_is_untrusted(run):
    ddl = "CREATE TABLE t (x int); INSERT INTO t VALUES (1), (2);"
    doc, _, _ = run(ddl, "SELECT x FROM t LIMIT 1", "SELECT x FROM t WHERE x = 99")
    assert doc["status"] == UNTRUSTED and doc["flags"]["not_deterministic"]


def test_replay_reproduces_hard_data(run):
    ddl = """
    CREATE TYPE addr AS (street text, zip char(5));
    CREATE TABLE dept (id int GENERATED ALWAYS AS IDENTITY PRIMARY KEY, boss int);
    CREATE TABLE emp (id int PRIMARY KEY, dept_id int NOT NULL REFERENCES dept, mentor int REFERENCES emp,
                      tags text[], doc jsonb, raw bytea, home addr);
    ALTER TABLE dept ADD FOREIGN KEY (boss) REFERENCES emp;
    CREATE TABLE audit (msg text);
    INSERT INTO audit VALUES ('seeded');
    CREATE FUNCTION log() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN INSERT INTO audit VALUES ('emp ' || NEW.id); RETURN NEW; END $$;
    CREATE TRIGGER log AFTER INSERT ON emp FOR EACH ROW EXECUTE FUNCTION log();
    """
    doc, _, _ = run(ddl, "SELECT e.id, d.boss FROM emp e JOIN dept d ON d.id = e.dept_id",
                    "SELECT e.id, d.boss FROM emp e JOIN dept d ON d.id = e.dept_id WHERE e.mentor IS NOT NULL")
    assert doc["data"]["replay_verified"] is True, doc["data"]["replay_note"]
    assert sorted(r[0] for r in doc["data"]["tables"]["public.audit"]["rows"]) == ["emp 1", "seeded"]
    script = doc["data"]["replay_sql"]
    assert "session_replication_role" not in script                      # no superuser needed
    assert "OVERRIDING SYSTEM VALUE" in script and 'ALTER TABLE "public"."emp" DISABLE TRIGGER USER;' in script
    assert script.index('INSERT INTO "public"."dept"') < script.index('INSERT INTO "public"."emp"')   # parents first
    assert "NOT DEFERRABLE;" in script                                 # cycle FKs set back afterwards
    assert doc["environment"]["collation"] == "C"


def test_verify_catches_a_replay_that_does_not_reproduce(settings):
    ddl = "CREATE TABLE t (x int);"
    with SchemaWorkspace(ddl, settings) as ws:
        with ws.conn.cursor() as cur:
            cur.execute("INSERT INTO t VALUES (1)")
        ws.conn.commit()
        r1, r2 = run_pair(ws.conn, "SELECT x FROM t", "SELECT x FROM t", 100)
        expected = compare(r1, r2)
    ok, why = verify(settings, ddl, "BEGIN; INSERT INTO t VALUES (2); COMMIT;", "SELECT x FROM t",
                     "SELECT x FROM t", expected)
    assert ok is False and "different result" in why
    ok, why = verify(settings, ddl, "BEGIN; INSERT INTO nope VALUES (1); COMMIT;", "SELECT 1", "SELECT 1", expected)
    assert ok is None and "did not run" in why


def test_a_query_failing_is_not_a_counterexample_and_the_search_goes_on(run):
    # x = 1 makes Q1 divide by zero: a crash, not a silent wrong answer
    doc, _, _ = run(T, "SELECT 10 / (x - 1) FROM t", "SELECT 10 / nullif(x - 1, 0) FROM t")
    assert doc["status"] == TO_MUTATION and doc["starting_point"] == "one_query_fails"
    assert doc["result"]["kind"] == "error" and "22012" in doc["flags"]["query_fails_on_data"][0]
    assert "first change the data that makes a query fail" in doc["mutation"]["note"]
    assert doc["data"]["replay_verified"] is True


def test_column_order_only_is_not_a_counterexample_and_goes_on(run):
    # x > 5 makes the base row (1, 6): swapping the columns gives (6, 1), the same values in another order
    doc, _, _ = run("CREATE TABLE t (id int PRIMARY KEY, x int CHECK (x > 5));", "SELECT * FROM t", "SELECT x, id FROM t")
    assert doc["status"] == COLUMN_ORDER_ONLY and doc["starting_point"] == "column_order_only"
    assert doc["mutation"]["actions"] == MUTATION_ACTIONS             # still handed to the mutation stage
