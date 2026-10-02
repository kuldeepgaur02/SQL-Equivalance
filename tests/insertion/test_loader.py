"""Step 6 end to end: INSERT, each branch of the repair loop, skips, UPDATEs, read-back."""
import json
from decimal import Decimal

import pytest

from cexgen.input import Case
from cexgen.insertion.model import CODE, LLM, RULES, RULES_AFTER_LLM
from cexgen.journal import Journal
from cexgen.llm import LLMCallFailed, LLMResult, register_provider
from cexgen.memory import SchemaMemory
from cexgen.runner import run_cases
from cexgen.schema import QName

pytestmark = pytest.mark.db


def q(name):
    return QName("public", name)


class ScriptedLLM:
    """A stand-in LLM API: answers repair requests from a script, records the prompts."""
    name = "scripted"
    script: list = []
    prompts: list = []

    def complete_json(self, system, prompt, schema, purpose):
        ScriptedLLM.prompts.append(prompt)
        if not ScriptedLLM.script:
            raise LLMCallFailed("nothing scripted")
        answer = ScriptedLLM.script.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return LLMResult(answer, "scripted-model", {"input_tokens": 0, "output_tokens": 0})


register_provider("scripted", lambda s: ScriptedLLM())


@pytest.fixture
def load(settings, tmp_path):
    """load(ddl, q1, llm=False, script=None) -> (LoadResult, journal entries, case run)"""
    def run(ddl, q1, llm=False, script=None, runs_dir=None):
        ScriptedLLM.script, ScriptedLLM.prompts = list(script or []), []
        s = settings.with_(llm_enabled=llm, llm_provider="scripted", runs_dir=str(runs_dir or tmp_path))
        [srun] = run_cases([Case("c", ddl, q1, q1)], s)
        [c] = srun.cases
        assert c.status == "ok", c.error
        return c.state["load"], Journal.load(c.journal_path), srun
    return run


def test_clean_load_reads_back_what_postgres_stored(load):
    result, entries, _ = load("""
    CREATE TYPE mood AS ENUM ('sad', 'ok');
    CREATE TYPE addr AS (street text, zip char(5));
    CREATE TABLE t (
        id int GENERATED ALWAYS AS IDENTITY PRIMARY KEY, code char(3), price numeric(6,2),
        moods mood[], doc jsonb, raw bytea, home addr, twice int GENERATED ALWAYS AS (id * 2) STORED
    );
    """, "SELECT * FROM t")
    row = result.rows[q("t")]
    assert row["id"] == 1 and row["twice"] == 2                     # identity ALWAYS inserted, generated computed
    assert row["code"] == "a  " and row["price"] == Decimal("1.00")
    assert row["doc"] == {} and bytes(row["raw"]) == b"\x00"
    assert result.repairs == () and result.skipped == {}
    assert any(e.step == "insert" and e.status == "ok" for e in entries)


def test_unique_collision_with_seed_row_fixed_by_code(load):
    result, _, _ = load("""
    CREATE TABLE t (id int PRIMARY KEY, name text UNIQUE);
    INSERT INTO t VALUES (1, 'a');
    """, "SELECT * FROM t")
    [r1, r2] = result.repairs
    assert (r1.category, r1.method, r1.changed["id"]) == ("unique", CODE, (1, 2))     # PK: max + 1
    assert (r2.category, r2.method, r2.changed["name"]) == ("unique", CODE, ("a", "a2"))
    assert result.rows[q("t")]["id"] == 2
    assert len(result.snapshot[q("t")]) == 2                        # the seed row is still there


def test_children_follow_a_repaired_parent_key(load):
    result, _, _ = load("""
    CREATE TABLE p (id int PRIMARY KEY);
    INSERT INTO p VALUES (1);
    CREATE TABLE c (id int, p_id int NOT NULL REFERENCES p);
    """, "SELECT * FROM c")
    assert result.rows[q("p")]["id"] == 2 and result.rows[q("c")]["p_id"] == 2


def test_check_not_understood_repaired_by_rules(load):
    result, _, _ = load("CREATE TABLE t (x int CHECK (x % 2 = 0 AND x > 5));", "SELECT * FROM t")
    [r] = result.repairs
    assert (r.category, r.method) == ("check", RULES) and result.rows[q("t")]["x"] == 6   # smallest even > 5


def test_check_repaired_by_the_llm(load):
    answer = {"note": "x must be even and above 5", "row": [{"column": "x", "value_json": "8"}]}
    result, entries, _ = load("CREATE TABLE t (x int CHECK (x % 2 = 0 AND x > 5));", "SELECT * FROM t",
                              llm=True, script=[answer])
    [r] = result.repairs
    assert (r.method, r.changed["x"], r.note) == (LLM, (1, 8), "x must be even and above 5")
    prompt = ScriptedLLM.prompts[0]
    assert "t_x_check" in prompt and "23514" in prompt and "CHECK" in prompt
    call = next(e for e in entries if e.llm)
    assert call.step == "repair" and call.status == "ok"


def test_bad_llm_answer_falls_back_to_rules(load):
    answer = {"note": "", "row": [{"column": "x", "value_json": '"not a number"'}]}
    result, _, _ = load("CREATE TABLE t (x int CHECK (x % 2 = 0 AND x > 5));", "SELECT * FROM t",
                        llm=True, script=[answer])
    assert result.repairs[0].method == RULES_AFTER_LLM and result.rows[q("t")]["x"] == 6


def test_llm_sees_earlier_attempts(load):
    wrong = {"note": "try 7", "row": [{"column": "x", "value_json": "7"}]}
    right = {"note": "even", "row": [{"column": "x", "value_json": "8"}]}
    result, _, _ = load("CREATE TABLE t (x int CHECK (x % 2 = 0 AND x > 5));", "SELECT * FROM t",
                        llm=True, script=[wrong, right])
    assert [r.method for r in result.repairs] == [LLM, LLM] and result.rows[q("t")]["x"] == 8
    assert '{"x": 7}' in ScriptedLLM.prompts[1]                     # the failed attempt is in the next prompt


TRIGGER = """
CREATE FUNCTION refuse() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'no rows here'; END $$;
CREATE TABLE p (id int PRIMARY KEY);
CREATE TRIGGER refuse BEFORE INSERT ON p FOR EACH ROW EXECUTE FUNCTION refuse();
CREATE TABLE needs (id int, p_id int NOT NULL REFERENCES p);
CREATE TABLE maybe (id int, p_id int REFERENCES p);
"""


def test_skip_after_failed_repairs_cascades(load):
    result, entries, _ = load(TRIGGER, "SELECT * FROM needs, maybe")
    assert "no repair found for trigger" in result.skipped[q("p")]
    assert "needs a row in public.p" in result.skipped[q("needs")]
    assert result.rows[q("maybe")]["p_id"] is None                  # nullable FK: NULL instead
    assert any("left NULL" in w for w in result.warnings)


def test_three_repairs_then_skip(load):
    bad = {"note": "", "row": [{"column": "x", "value_json": "1"}]}
    attempts = [{"note": "", "row": [{"column": "x", "value_json": str(v)}]} for v in (3, 5, 7)]
    result, entries, _ = load("CREATE TABLE t (x int CHECK (x % 2 = 0 AND x > 5));", "SELECT * FROM t",
                              llm=True, script=attempts + [bad])
    assert [r.attempt for r in result.repairs] == [1, 2, 3]
    assert "still failing after 3 repairs" in result.skipped[q("t")]
    assert sum(e.step == "insert" and e.status == "failed" for e in entries) == 4


def test_cycle_repaired_inside_its_transaction(load):
    result, _, _ = load("""
    CREATE TABLE a (id int PRIMARY KEY, b_id int NOT NULL, x int CHECK (x % 3 = 0 AND x > 0));
    CREATE TABLE b (id int PRIMARY KEY, a_id int NOT NULL REFERENCES a);
    ALTER TABLE a ADD FOREIGN KEY (b_id) REFERENCES b;
    """, "SELECT * FROM a")
    x = result.rows[q("a")]["x"]
    assert x is not None and x % 3 == 0 and x > 0 and result.repairs[0].method == RULES
    assert result.rows[q("a")]["b_id"] == result.rows[q("b")]["id"]
    assert result.rows[q("b")]["a_id"] == result.rows[q("a")]["id"]


def test_trigger_side_effects_show_in_the_snapshot(load):
    result, _, _ = load("""
    CREATE TABLE audit (msg text);
    CREATE TABLE t (x int);
    CREATE FUNCTION log() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN INSERT INTO audit VALUES ('inserted ' || NEW.x); NEW.x := NEW.x + 41; RETURN NEW; END $$;
    CREATE TRIGGER log BEFORE INSERT ON t FOR EACH ROW EXECUTE FUNCTION log();
    """, "SELECT * FROM t")
    assert result.rows[q("t")]["x"] == 42                           # what Postgres stored, not what we sent
    assert result.snapshot[q("audit")] == ({"msg": "inserted 1"},)


def test_materialized_views_refreshed_after_load(settings, tmp_path):
    seen = []

    def count_view(ctx):
        with ctx.conn.cursor() as cur:
            cur.execute("SELECT n FROM total")
            seen.append(cur.fetchone()[0])
        ctx.conn.rollback()

    from cexgen.runner import PIPELINE
    s = settings.with_(llm_enabled=False, runs_dir=str(tmp_path))
    run_cases([Case("c", "CREATE TABLE t (x int); CREATE MATERIALIZED VIEW total AS SELECT count(*) AS n FROM t;",
                    "SELECT * FROM total, t", "SELECT * FROM t")], s, pipeline=PIPELINE + [count_view])
    assert seen == [1]                                              # the view sees the loaded row


def test_memory_keeps_good_rows_and_failed_fixes(load, settings, tmp_path):
    attempts = [{"note": "", "row": [{"column": "x", "value_json": "7"}]},
                {"note": "", "row": [{"column": "x", "value_json": "8"}]}]
    _, _, srun = load("CREATE TABLE t (x int CHECK (x % 2 = 0 AND x > 5));", "SELECT * FROM t",
                      llm=True, script=attempts, runs_dir=tmp_path)
    data = json.loads(srun.memory_path.read_text())["facts"]
    assert data["base_rows"]["public.t"] == {"x": 8}
    assert {"x": 7} in next(iter(data["failed_fixes"].values()))
    result, _, _ = load("CREATE TABLE t (x int CHECK (x % 2 = 0 AND x > 5));", "SELECT * FROM t",
                        llm=True, script=[], runs_dir=tmp_path)
    assert result.repairs == () and result.rows[q("t")]["x"] == 8   # next run starts from the known-good row


def test_partition_error_repaired_by_llm(load):
    answer = {"note": "month 2024-03", "row": [{"column": "d", "value_json": '"2024-03-15 10:00:00"'}]}
    result, _, _ = load("""
    CREATE TABLE ev (d timestamp NOT NULL) PARTITION BY RANGE (date_trunc('month', d));
    CREATE TABLE ev_mar PARTITION OF ev FOR VALUES FROM ('2024-03-01') TO ('2024-04-01');
    """, "SELECT * FROM ev", llm=True, script=[answer])
    assert result.repairs[0].category == "partition" and result.repairs[0].method == LLM
    assert str(result.rows[q("ev")]["d"]) == "2024-03-15 10:00:00"
