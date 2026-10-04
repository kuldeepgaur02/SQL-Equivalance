"""The VeriEQL importer, on a miniature copy of the repository layout (no download needed)."""
import json

import pytest

from cexgen.config import Settings
from cexgen.input import load_cases
from cexgen.input.veriEQL import quote_dollar_names, schema_to_sql, translate


def col(ref):
    return {"value": ref}


SCHEMA = {
    "PERSON": {"PERSONID": "INT", "NAME": "VARCHAR", "SEX": "ENUM,M,F", "BORN": "DATE", "SCORE": "NUMERIC"},
    "ADDRESS": {"ADDRESSID": "INT", "PERSONID": "INT", "CITY": "VARCHAR", "KIND": "ENUM,HOME,NULL"},
    "TAGS": {"A": "INT", "B": "INT", "C": "BOOL"},
}
CONSTRAINTS = [
    {"primary": [col("PERSON__PERSONID")]},
    {"primary": [col("ADDRESS__ADDRESSID")]},
    {"primary": [col("ADDRESS__PERSONID")]},                      # a second key: UNIQUE + NOT NULL
    {"primary": [col("TAGS__A"), col("TAGS__B")]},                 # composite key
    {"foreign": [col("ADDRESS__PERSONID"), col("PERSON__PERSONID")]},
    {"foreign": [col("TAGS__A"), col("ADDRESS__CITY")]},          # parent is not a key: not enforced
    {"not_null": col("PERSON__NAME")},
    {"gte": [col("PERSON__SCORE"), 0]},
    {"between": [col("TAGS__B"), 1, 5]},
    {"in": [col("TAGS__C"), [0, 1]]},
    {"lte": [col("PERSON__BORN"), {"date": "2019-07-05"}]},
    {"neq": [col("TAGS__A"), col("TAGS__B")]},
    {"imply": [{"eq": [col("PERSON__PERSONID"), col("ADDRESS__PERSONID")]}, {"lt": [col("PERSON__BORN"), 1]}]},
    {"consec": col("TAGS__A")},
]


def test_schema_and_constraints_become_postgres():
    ddl, notes = schema_to_sql(SCHEMA, CONSTRAINTS)
    assert "CREATE TABLE person (" in ddl and "personid integer NOT NULL" in ddl
    assert "sex varchar" in ddl and "CHECK (sex IN ('M', 'F'))" in ddl
    assert "CHECK (kind IN ('HOME'))" in ddl and "kind varchar NOT NULL" not in ddl     # NULL label: nullable
    assert "PRIMARY KEY (addressid)" in ddl and "UNIQUE (personid)" in ddl
    assert "PRIMARY KEY (a, b)" in ddl
    assert "ALTER TABLE address ADD FOREIGN KEY (personid) REFERENCES person (personid);" in ddl
    assert "score numeric NOT NULL" in ddl and "CHECK (score >= 0)" in ddl              # VeriEQL: NULL fails >=
    assert "b integer NOT NULL" in ddl and "CHECK (b BETWEEN 1 AND 5)" in ddl
    assert "CHECK (born <= DATE '2019-07-05')" in ddl
    assert "CHECK (a <> b)" in ddl
    assert "c boolean NOT NULL" in ddl and "CHECK (c IN (FALSE, TRUE))" in ddl          # Calcite-style boolean
    mysql_ddl, _ = schema_to_sql(SCHEMA, CONSTRAINTS, "mysql")
    assert "c smallint NOT NULL" in mysql_ddl and "CHECK (c IN (0, 1))" in mysql_ddl   # MySQL BOOL = TINYINT(1)
    text = " ".join(notes)
    assert "tags.a -> address.city not enforced" in text.lower()
    assert "imply" in text and "consec" in text


def test_mysql_translation_keeps_mysql_meaning():
    q = translate("SELECT ROUND(SUM(a > 1) / COUNT(*), 2) AS `Rate`, CONCAT(lat, lon) FROM `Orders` LIMIT 1, 1",
                  "mysql")
    assert "CASE WHEN a > 1 THEN 1 ELSE 0 END" in q
    assert "ROUND(CAST(" in q and "NULLIF(COUNT(*), 0)" in q
    assert "CAST(lat AS TEXT) || CAST(lon AS TEXT)" in q
    assert "FROM orders" in q and "AS rate" in q and "LIMIT 1 OFFSET 1" in q
    assert translate("SELECT `user` FROM t", "mysql") == 'SELECT "user" FROM t'
    assert translate("SELECT (((", "mysql") == "SELECT ((("                             # unparseable: unchanged


def test_calcite_dollar_names_are_quoted():
    assert quote_dollar_names("SELECT t.EXPR$0, 1 AS $f1 FROM x AS t ($2) WHERE y = '$f1'") == \
        "SELECT t.EXPR$0, 1 AS \"$f1\" FROM x AS t (\"$2\") WHERE y = '$f1'"


@pytest.fixture
def mini_repo(tmp_path):
    """benchmarks/leetcode/leetcode.jsonlines + experiments/<date>/leetcode.out, like the real repo."""
    bench = tmp_path / "benchmarks" / "leetcode"
    bench.mkdir(parents=True)
    pairs = [
        ["SELECT NAME FROM PERSON", "SELECT DISTINCT NAME FROM PERSON"],
        ["SELECT NAME FROM PERSON WHERE SCORE > 1", "SELECT NAME FROM PERSON WHERE SCORE >= 1"],
        ["SELECT 1", "SELECT 2; SELECT 3"],                                               # invalid: 2 statements
        ["SELECT CITY FROM ADDRESS", "SELECT CITY FROM ADDRESS"],
    ]
    rows = [{"file": "raw/1.csv", "index": i % 2, "schema": SCHEMA, "constraint": None if i == 3 else CONSTRAINTS,
             "pair": p} for i, p in enumerate(pairs)]                                     # index is NOT unique
    (bench / "leetcode.jsonlines").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    for run, states in (("2023_01_01", ["EQU"]), ("2025_10_31", None)):
        out = tmp_path / "experiments" / run
        out.mkdir(parents=True)
        results = [
            {"index": 0, "pair": pairs[0], "states": states or ["EQU", "NEQ"], "counterexample": "INSERT ...",
             "err": "Symbolic reasoning: NOT EQUIVALENT."},
            {"index": 1, "pair": pairs[1], "states": ["EQU", "EQU"], "counterexample": None, "err": None},
            {"index": 1, "pair": pairs[3], "states": ["NIE"], "counterexample": None, "err": "Not supported: OVER"},
        ]
        (out / "leetcode.out").write_text("\n".join(json.dumps(r) for r in results) + "\n")
    return bench / "leetcode.jsonlines"


def test_loads_cases_with_verdicts_matched_by_pair(mini_repo, caplog):
    cases = load_cases(mini_repo, Settings())
    assert [c.name for c in cases] == ["veriEQL-leetcode-00001", "veriEQL-leetcode-00002", "veriEQL-leetcode-00004"]
    assert "1 of 4 entries could not be turned into cases" in caplog.text
    first, second, fourth = cases
    assert first.meta["veriEQL"]["verdict"] == "different" and first.meta["veriEQL"]["run"] == "2025_10_31"
    assert first.meta["veriEQL"]["counterexample"] == "INSERT ..."
    assert second.meta["veriEQL"]["verdict"] == "equivalent_bounded"
    assert fourth.meta["veriEQL"]["verdict"] == "undecided" and "OVER" in fourth.meta["veriEQL"]["note"]
    assert "not_enforced" not in fourth.meta                                    # constraint: null
    assert first.meta["original_dialect"] == "mysql" and first.q1 == "SELECT name FROM person"
    assert first.meta["original_pair"][0] == "SELECT NAME FROM PERSON"


def test_sample_is_fixed_by_seed(mini_repo):
    a = [c.name for c in load_cases(mini_repo, Settings(), sample=2, seed=7)]
    b = [c.name for c in load_cases(mini_repo, Settings(), sample=2, seed=7)]
    assert a == b and len(a) == 2 and a == sorted(a)                            # kept in file order
    assert len(load_cases(mini_repo, Settings(), sample=99)) == 3              # larger than the set: all


@pytest.mark.db
def test_imported_case_runs_through_the_pipeline(mini_repo, settings, tmp_path):
    from cexgen.runner import run_cases
    cases = load_cases(mini_repo, Settings(), sample=1, seed=0)
    [run] = run_cases(cases, settings.with_(llm_enabled=False, runs_dir=str(tmp_path / "runs")))
    assert run.cases[0].status == "ok", run.cases[0].error
    assert run.cases[0].state["load"].rows                                      # base rows inserted
