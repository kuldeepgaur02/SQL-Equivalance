"""Step 8: both results empty -> rebuild the base -> INSERT -> compare, at most max_rebuilds rounds."""
import json
from decimal import Decimal

import pytest

from cexgen.input import Case
from cexgen.journal import Journal
from cexgen.llm import LLMCallFailed, LLMResult, register_provider
from cexgen.runner import run_cases
from cexgen.schema import QName

pytestmark = pytest.mark.db


def q(name):
    return QName("public", name)


class ScriptedLLM:
    name = "scripted-rebuild"
    script: list = []
    prompts: list = []

    def complete_json(self, system, prompt, schema, purpose):
        ScriptedLLM.prompts.append(prompt)
        if not ScriptedLLM.script:
            raise LLMCallFailed("nothing scripted")
        return LLMResult(ScriptedLLM.script.pop(0), "scripted", {})


register_provider("scripted-rebuild", lambda s: ScriptedLLM())


@pytest.fixture
def run(settings, tmp_path):
    def go(ddl, q1, q2, llm=False, script=None, max_rebuilds=3):
        ScriptedLLM.script, ScriptedLLM.prompts = list(script or []), []
        s = settings.with_(llm_enabled=llm, llm_provider="scripted-rebuild", runs_dir=str(tmp_path),
                           max_rebuilds=max_rebuilds)
        [srun] = run_cases([Case("c", ddl, q1, q2)], s)
        [c] = srun.cases
        assert c.status == "ok", c.error
        return c.state, Journal.load(c.journal_path), srun
    return go


SHOP = """
CREATE TABLE users (id int PRIMARY KEY, name text NOT NULL, age int CHECK (age >= 18));
CREATE TABLE orders (id int PRIMARY KEY, user_id int NOT NULL REFERENCES users, amount numeric(8,2),
                     status text, meta jsonb);
"""


# No JSON / ARRAY columns: otherwise Step 5 would also ask the scripted LLM (for their values).
SHOP_NO_JSON = """
CREATE TABLE users (id int PRIMARY KEY, name text NOT NULL, age int CHECK (age >= 18));
CREATE TABLE orders (id int PRIMARY KEY, user_id int NOT NULL REFERENCES users, amount numeric(8,2), status text);
"""


def test_filters_are_written_into_the_base(run):
    state, entries, _ = run(SHOP, "SELECT o.id FROM orders o WHERE o.amount > 100 AND o.status = 'paid'",
                            "SELECT o.id FROM orders o WHERE o.amount >= 100 AND o.status = 'paid'")
    rounds = state["rounds"]
    assert [r.comparison.outcome for r in rounds] == ["both_empty", "same"]
    row = state["load"].rows[q("orders")]
    assert row["amount"] == Decimal("100.01") and row["status"] == "paid"
    assert rounds[1].method == "rules"
    rebuild = [e for e in entries if e.step == "rebuild"]
    assert rebuild and rebuild[-1].detail["comparison"]["outcome"] == "same"


def test_join_and_fk_filter_move_the_parent_key(run):
    state, _, _ = run(SHOP, "SELECT u.name FROM users u JOIN orders o ON o.user_id = u.id WHERE o.user_id = 7",
                      "SELECT u.name FROM users u JOIN orders o ON o.user_id = u.id WHERE u.id = 7")
    assert state["comparison"].outcome == "same"
    assert state["load"].rows[q("users")]["id"] == 7 and state["load"].rows[q("orders")]["user_id"] == 7


def test_json_path_with_cast(run):
    state, _, _ = run(SHOP, "SELECT id FROM orders WHERE (meta->>'qty')::int > 5 AND meta->>'channel' = 'web'",
                      "SELECT id FROM orders WHERE (meta->>'qty')::int >= 6 AND meta->>'channel' = 'web'")
    assert state["comparison"].outcome == "same"
    meta = state["load"].rows[q("orders")]["meta"]
    assert meta["channel"] == "web" and int(meta["qty"]) > 5


def test_a_filter_can_find_a_difference(run):
    state, _, _ = run(SHOP, "SELECT id FROM users WHERE age > 30", "SELECT id FROM users WHERE age > 40")
    # round 1 writes Q1's filter (age 31): Q1 returns the row, Q2 does not
    assert [r.comparison.outcome for r in state["rounds"]] == ["both_empty", "differ"]
    assert state["load"].rows[q("users")]["age"] == 31


def test_check_wins_over_a_filter_and_rounds_move_on(run):
    state, _, _ = run(SHOP, "SELECT id FROM users WHERE age < 10", "SELECT id FROM users WHERE age = 20")
    rounds = state["rounds"]
    # round 1: Q1's age < 10 conflicts with CHECK (age >= 18); round 2 uses Q2's filter
    assert any("schema rule" in n or "no value" in n for n in rounds[1].notes)
    assert state["comparison"].outcome == "differ" and state["load"].rows[q("users")]["age"] == 20


def test_stops_after_max_rebuilds(run):
    state, entries, _ = run(SHOP, "SELECT id FROM users WHERE id > 1 AND id < 1",
                            "SELECT id FROM users WHERE name = 'x' AND name = 'y'", max_rebuilds=2)
    assert state["comparison"].outcome == "both_empty"                      # handed on as both empty
    assert [r.number for r in state["rounds"]] == [0]                       # no value fits: nothing was run
    skipped = [e for e in entries if e.step == "rebuild" and e.status == "skipped"]
    assert len(skipped) == 2 and "no value satisfies" in " ".join(skipped[0].detail["notes"])


def test_llm_rebuild_is_used_and_sees_earlier_rounds(run):
    useless = {"note": "try 50", "tables": [{"table": "public.orders", "cells": [
        {"column": "amount", "value_json": "50"}]}]}
    good = {"note": "amount above 100, paid", "tables": [{"table": "orders", "cells": [
        {"column": "amount", "value_json": "150"}, {"column": "status", "value_json": '"paid"'}]}]}
    state, entries, _ = run(SHOP_NO_JSON, "SELECT id FROM orders WHERE amount > 100 AND status = 'paid'",
                            "SELECT id FROM orders WHERE amount >= 100 AND status = 'paid'",
                            llm=True, script=[useless, good])
    rounds = state["rounds"]
    assert [r.method for r in rounds] == ["base", "llm", "llm"]
    assert state["load"].rows[q("orders")]["amount"] == Decimal("150.00") and state["comparison"].outcome == "same"
    assert "Earlier rebuild 1" in ScriptedLLM.prompts[1] and '"amount": "50.00"' in ScriptedLLM.prompts[1]
    assert "orders.amount > 100" in ScriptedLLM.prompts[0]                 # Step 3's filters are in the prompt


def test_bad_llm_answer_falls_back_to_rules(run):
    bad = {"note": "", "tables": [{"table": "nope", "cells": []},
                                  {"table": "orders", "cells": [{"column": "amount", "value_json": '"lots"'}]}]}
    state, _, _ = run(SHOP_NO_JSON, "SELECT id FROM orders WHERE amount > 100",
                      "SELECT id FROM orders WHERE amount >= 100", llm=True, script=[bad])
    assert state["rounds"][1].method == "rules (llm failed)"
    assert "unknown or unplanned table 'nope'" in state["rounds"][1].notes[0]
    assert state["comparison"].outcome == "same"


def test_rebuilt_rows_do_not_go_into_schema_memory(run):
    _, _, srun = run(SHOP, "SELECT id FROM orders WHERE amount > 100", "SELECT id FROM orders WHERE amount >= 100")
    facts = json.loads(srun.memory_path.read_text())["facts"]
    assert facts["base_rows"]["public.orders"]["amount"] == "1.00"          # the base row, not 100.01


def test_not_both_empty_means_no_rebuild(run):
    state, entries, _ = run(SHOP, "SELECT id FROM users", "SELECT id FROM users")
    assert [r.number for r in state["rounds"]] == [0] and not [e for e in entries if e.step == "rebuild"]
