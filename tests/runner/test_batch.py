import pytest

from cexgen.errors import InputError
from cexgen.input import Case
from cexgen.journal import Journal
from cexgen.runner import run_cases

pytestmark = pytest.mark.db

SCHEMA = "CREATE TABLE t (x int); INSERT INTO t VALUES (1);"


@pytest.fixture
def run_settings(settings, tmp_path):
    return settings.with_(runs_dir=str(tmp_path))


def case(name, schema=SCHEMA):
    return Case(name, schema, "SELECT x FROM t", "SELECT x FROM t WHERE x > 0")


def test_one_database_per_schema_and_a_journal_per_case(run_settings):
    runs = run_cases([case("a"), case("b"), case("c", "CREATE TABLE u (y int);")], run_settings, pipeline=[])
    assert [len(r.cases) for r in runs] == [2, 1]
    assert runs[0].database != runs[1].database
    for r in runs:
        for c in r.cases:
            assert c.status == "ok"
            steps = [(e.step, e.status) for e in Journal.load(c.journal_path)]
            assert steps[0] == ("run", "info") and steps[-1] == ("run", "ok")


def test_cases_do_not_see_each_others_rows(run_settings):
    seen = []

    def write_then_count(ctx):
        with ctx.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM t")
            seen.append(cur.fetchone()[0])
            cur.execute("INSERT INTO t VALUES (2), (3)")
        ctx.conn.commit()

    run_cases([case("a"), case("b"), case("c")], run_settings, pipeline=[write_then_count])
    assert seen == [1, 1, 1]        # each case starts with only the seed row


def test_failing_step_fails_only_its_case(run_settings):
    def step(ctx):
        if ctx.case.name == "b":
            raise InputError("Q1 is not a SELECT")

    [run] = run_cases([case("a"), case("b"), case("c")], run_settings, pipeline=[step])
    assert [c.status for c in run.cases] == ["ok", "failed", "ok"]
    last = Journal.load(run.cases[1].journal_path)[-1]
    assert last.status == "failed" and last.error["message"] == "Q1 is not a SELECT"


def test_crash_in_a_step_is_journaled_and_batch_continues(run_settings):
    def step(ctx):
        if ctx.case.name == "a":
            raise ZeroDivisionError("bug")

    [run] = run_cases([case("a"), case("b")], run_settings, pipeline=[step])
    assert [c.status for c in run.cases] == ["failed", "ok"]
    assert "traceback" in Journal.load(run.cases[0].journal_path)[-1].error


def test_broken_schema_fails_its_cases_only(run_settings):
    runs = run_cases([case("bad", "CREATE TABLE t (x nosuchtype);"), case("good")], run_settings, pipeline=[])
    assert runs[0].error and [c.status for c in runs[0].cases] == ["failed"]
    assert Journal.load(runs[0].cases[0].journal_path)[-1].error["sqlstate"] == "42704"
    assert runs[1].cases[0].status == "ok"


def test_memory_written_by_a_step_survives_to_the_next_run(run_settings):
    def learn(ctx):
        ctx.memory.put("base_rows", "public.t", {"x": 42})

    run_cases([case("a")], run_settings, pipeline=[learn])
    seen = []
    run_cases([case("b")], run_settings, pipeline=[lambda ctx: seen.append(ctx.memory.get("base_rows", "public.t"))])
    run_cases([case("c")], run_settings.with_(use_schema_memory=False),
              pipeline=[lambda ctx: seen.append(ctx.memory.get("base_rows", "public.t"))])
    assert seen == [{"x": 42}, None]


def test_schema_model_is_in_the_pipeline_and_read_once_per_schema(run_settings):
    from cexgen.runner import PIPELINE
    from cexgen.schema import parse_schema

    assert PIPELINE[0] is parse_schema
    [run] = run_cases([case("a"), case("b")], run_settings)
    models = [c.state["schema"] for c in run.cases]
    assert models[0] is models[1] and models[0].table("t").seed_rows == 1
    cached = [next(e for e in Journal.load(c.journal_path) if e.step == "parse_schema").detail["cached"]
              for c in run.cases]
    assert cached == [False, True]


def test_queries_step_follows_schema_step(run_settings):
    from cexgen.queries import parse_queries
    from cexgen.runner import PIPELINE

    assert PIPELINE[1] is parse_queries
    bad = Case("bad", SCHEMA, "SELECT nope FROM t", "SELECT x FROM t")
    [run] = run_cases([case("a"), bad], run_settings)
    assert run.cases[0].state["queries"].q1.predicates == ()
    assert run.cases[0].state["queries"].q2.predicates[0].op == ">"
    assert run.cases[1].status == "failed"
    assert Journal.load(run.cases[1].journal_path)[-1].error["sqlstate"] == "42703"


def test_plan_step_follows_queries_step(run_settings):
    from cexgen.ordering import plan_order
    from cexgen.runner import PIPELINE

    assert PIPELINE[2] is plan_order
    schema = SCHEMA + " CREATE TABLE other (y int);"
    [run] = run_cases([Case("a", schema, "SELECT x FROM t", "SELECT x FROM t WHERE x > 0")], run_settings)
    plan = run.cases[0].state["plan"]
    assert [str(t) for t in plan.order] == ["public.t"] and "public.other" in map(str, plan.skipped)
    entry = next(e for e in Journal.load(run.cases[0].journal_path) if e.step == "order")
    assert entry.detail["not_needed"] == ["public.other"]


def test_base_data_step_follows_plan_step(run_settings):
    from cexgen.basedata import build_base_data
    from cexgen.runner import PIPELINE

    assert PIPELINE[3] is build_base_data
    [run] = run_cases([case("a")], run_settings.with_(llm_enabled=False))
    base = run.cases[0].state["base"]
    assert base.values(next(iter(base.rows))) == {"x": 1}
    entry = next(e for e in Journal.load(run.cases[0].journal_path) if e.step == "base_data")
    assert entry.detail["mode"] == "rules" and entry.detail["rows"]["public.t"]["x"]["source"] == "default"


def test_insert_step_follows_base_data_step(run_settings):
    from cexgen.insertion import insert_base
    from cexgen.runner import PIPELINE

    assert PIPELINE[4] is insert_base
    [run] = run_cases([case("a")], run_settings.with_(llm_enabled=False))
    load = run.cases[0].state["load"]
    assert len(load.snapshot[next(iter(load.snapshot))]) == 2        # the seed row + the base row
