import json
from pathlib import Path

import pytest

from cexgen.config import Settings
from cexgen.errors import InputError
from cexgen.input import load_cases

EXAMPLES = Path(__file__).parents[2] / "examples"
SCHEMA = "CREATE TABLE t (x int);"


def folder_case(root: Path, name="c1", schema=SCHEMA, q1="SELECT x FROM t", q2="SELECT 1 FROM t") -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "schema.sql").write_text(schema)
    (d / "q1.sql").write_text(q1)
    (d / "q2.sql").write_text(q2)
    return d


def batch(root: Path, spec: dict, name="b.json") -> Path:
    p = root / name
    p.write_text(json.dumps(spec))
    return p


# -- folder ---------------------------------------------------------------------
def test_example_folder():
    [case] = load_cases(EXAMPLES / "shop")
    assert case.name == "shop" and case.meta["expected"] == "different"
    assert not case.q1.endswith(";")


def test_folder_missing_files_listed(tmp_path):
    d = tmp_path / "c"
    d.mkdir()
    (d / "schema.sql").write_text(SCHEMA)
    with pytest.raises(InputError, match="missing q1.sql, q2.sql"):
        load_cases(d)


def test_bom_is_stripped(tmp_path):
    d = folder_case(tmp_path)
    (d / "q1.sql").write_bytes(b"\xef\xbb\xbfSELECT x FROM t")
    assert load_cases(d)[0].q1 == "SELECT x FROM t"


def test_invalid_utf8_reported_with_line(tmp_path):
    d = folder_case(tmp_path)
    (d / "q2.sql").write_bytes(b"SELECT 1\nFROM t WHERE x = '\xff'")
    with pytest.raises(InputError, match="not valid UTF-8.*line 2"):
        load_cases(d)


def test_size_limit(tmp_path):
    d = folder_case(tmp_path, schema=SCHEMA + " " * 200)
    with pytest.raises(InputError, match="over the 100-byte limit"):
        load_cases(d, Settings(max_input_bytes=100))


def test_bad_meta_json(tmp_path):
    d = folder_case(tmp_path)
    (d / "meta.json").write_text("{not json")
    with pytest.raises(InputError, match="invalid JSON"):
        load_cases(d)


# -- batch ----------------------------------------------------------------------
def test_example_batch_shares_schema_and_merges_meta():
    cases = load_cases(EXAMPLES / "shop_batch.json")
    assert len(cases) == 3
    assert len({c.schema_sql for c in cases}) == 1
    assert cases[0].meta == {"suite": "shop", "expected": "different"}
    assert cases[2].identical_queries


def test_batch_query_files_relative_to_batch(tmp_path):
    (tmp_path / "q").mkdir()
    (tmp_path / "q" / "a.sql").write_text("SELECT x FROM t;")
    p = batch(tmp_path, {"schema": SCHEMA, "pairs": [{"q1_file": "q/a.sql", "q2": "SELECT 2"}]})
    [case] = load_cases(p)
    assert case.q1 == "SELECT x FROM t" and case.name == "b#1"


@pytest.mark.parametrize("spec, msg", [
    ({"pairs": [{"q1": "SELECT 1", "q2": "SELECT 2"}]}, "exactly one of 'schema'"),
    ({"schema": SCHEMA, "schema_file": "x.sql", "pairs": []}, "exactly one of 'schema'"),
    ({"schema": SCHEMA, "pairs": []}, "non-empty list"),
    ({"schema": SCHEMA, "pairs": [{"q1": "SELECT 1"}]}, "exactly one of 'q2'"),
    ({"schema": SCHEMA, "pairs": [{"q1": "SELECT 1", "q1_file": "a", "q2": "SELECT 2"}]}, "exactly one of 'q1'"),
    ({"schema": SCHEMA, "pairs": [{"querry1": "SELECT 1", "q1": "SELECT 1", "q2": "SELECT 2"}]}, "unknown key"),
    ({"schema": SCHEMA, "pairs": [{"name": "a", "q1": "SELECT 1", "q2": "SELECT 2"},
                                  {"name": "a", "q1": "SELECT 1", "q2": "SELECT 3"}]}, "already used"),
    ({"schema": SCHEMA, "pairs": ["SELECT 1"]}, "must be an object"),
    ({"schema": SCHEMA, "pairs": [{"q1": "SELECT 1", "q2": "SELECT 2", "meta": []}]}, "'meta' must be an object"),
])
def test_batch_validation(tmp_path, spec, msg):
    with pytest.raises(InputError, match=msg):
        load_cases(batch(tmp_path, spec))


def test_batch_must_be_object(tmp_path):
    p = tmp_path / "b.json"
    p.write_text("[1, 2]")
    with pytest.raises(InputError, match="JSON object"):
        load_cases(p)


# -- suite ----------------------------------------------------------------------
def test_suite_recurses_and_skips_hidden(tmp_path):
    folder_case(tmp_path, "a")
    folder_case(tmp_path / "nested", "b")
    folder_case(tmp_path, ".hidden")
    folder_case(tmp_path, "_draft")
    batch(tmp_path, {"schema": SCHEMA, "pairs": [{"name": "c", "q1": "SELECT 1", "q2": "SELECT 2"}]})
    (tmp_path / "notes.txt").write_text("ignored")
    assert sorted(c.name for c in load_cases(tmp_path)) == ["a", "b", "c"]


def test_suite_duplicate_names(tmp_path):
    folder_case(tmp_path, "a")
    batch(tmp_path, {"schema": SCHEMA, "pairs": [{"name": "a", "q1": "SELECT 1", "q2": "SELECT 2"}]})
    with pytest.raises(InputError, match="duplicate case name 'a'"):
        load_cases(tmp_path)


def test_empty_folder_and_missing_path(tmp_path):
    with pytest.raises(InputError, match="no cases found"):
        load_cases(tmp_path)
    with pytest.raises(InputError, match="no such file"):
        load_cases(tmp_path / "nope")


def test_whole_examples_folder():
    assert len(load_cases(EXAMPLES)) == 6          # shop, cycle, rich_departments, and 3 batch pairs
