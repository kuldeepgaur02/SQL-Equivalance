import datetime as dt
import json
from decimal import Decimal

import psycopg2
import pytest

from cexgen.errors import InputError
from cexgen.journal import Journal, case_dirname, to_jsonable


def test_entries_written_as_they_happen_and_read_back(tmp_path):
    path = tmp_path / "c" / "log.jsonl"
    j = Journal("c", path)
    j.record("insert", "users row", "failed", error={"sqlstate": "23514", "message": "check"})
    # before close(): the line is already on disk
    assert len(path.read_text().splitlines()) == 1
    j.record("repair", "age -> 18", detail={"row": {"age": 18}})
    j.close()
    entries = Journal.load(path)
    assert [e.seq for e in entries] == [1, 2]
    assert entries[0].error["sqlstate"] == "23514" and entries[1].detail == {"row": {"age": 18}}


def test_torn_last_line_is_skipped(tmp_path):
    path = tmp_path / "log.jsonl"
    with Journal("c", path) as j:
        j.record("insert", "a")
    with open(path, "a") as f:
        f.write('{"seq": 2, "case": "c", "st')          # crash mid-write
    assert len(Journal.load(path)) == 1


def test_timed_records_duration_and_failure():
    j = Journal("c")
    with j.timed("compare", "run Q1/Q2") as d:
        d["q1_rows"] = 3
    with pytest.raises(InputError):
        with j.timed("insert", "row"):
            raise InputError("bad")
    ok, failed = j.entries
    assert ok.status == "ok" and ok.detail == {"q1_rows": 3} and ok.duration_ms >= 0
    assert failed.status == "failed" and failed.error == {"type": "InputError", "message": "bad"}
    assert j.failures() == [failed] and j.last("compare") == ok


def test_postgres_error_details_are_captured():
    conn = None
    err = psycopg2.errors.CheckViolation("new row violates check constraint")
    entry = Journal("c").record("insert", "x", "failed", error=err)
    assert entry.error["type"] == "CheckViolation"
    del conn


def test_unexpected_exception_keeps_traceback():
    try:
        {}["missing"]
    except KeyError as e:
        entry = Journal("c").record("run", "crash", "failed", error=e)
    assert "traceback" in entry.error


def test_unknown_step_or_status_rejected():
    with pytest.raises(ValueError):
        Journal("c").record("mutate_everything", "x")
    with pytest.raises(ValueError):
        Journal("c").record("insert", "x", "maybe")


def test_values_become_lossless_json():
    value = {"n": Decimal("1.500"), "big": 2 ** 70, "when": dt.date(2024, 1, 2), "raw": b"\x00\xff",
             "nan": float("nan"), "s": {3, 1}}
    out = to_jsonable(value)
    json.dumps(out)
    assert out["n"] == "1.500" and out["big"] == str(2 ** 70) and out["raw"] == {"base64": "AP8="}
    assert out["nan"] == "nan" and out["s"] == [1, 3]


def test_case_dirname_is_safe_and_unique():
    assert case_dirname("a:b") != case_dirname("a_b")
    name = case_dirname("../../etc/passwd")
    assert "/" not in name and not name.startswith(".")


def test_journal_for_case_uses_runs_dir(tmp_path):
    with Journal.for_case("shop:gt", tmp_path) as j:
        j.record("run", "start", "info")
    assert j.path.parent.parent == tmp_path and j.path.suffix == ".jsonl"
