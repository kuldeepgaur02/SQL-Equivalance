import json
from decimal import Decimal

import pytest

from cexgen.config import Settings
from cexgen.memory import SchemaMemory


@pytest.fixture
def settings(tmp_path):
    return Settings(runs_dir=str(tmp_path))


def test_saved_knowledge_is_reused_by_the_next_run(settings):
    m = SchemaMemory.open("abc", 160004, settings)
    m.put("base_rows", "public.users", {"age": 18, "score": Decimal("1.50")})
    m.save()
    again = SchemaMemory.open("abc", 160004, settings)
    assert again.get("base_rows", "public.users") == {"age": 18, "score": "1.50"}


def test_changed_schema_or_server_major_does_not_reuse(settings):
    m = SchemaMemory.open("abc", 160004, settings)
    m.put("base_rows", "t", 1)
    m.save()
    assert SchemaMemory.open("other", 160004, settings).get("base_rows", "t") is None
    assert SchemaMemory.open("abc", 150014, settings).get("base_rows", "t") is None
    assert SchemaMemory.open("abc", 160009, settings).get("base_rows", "t") == 1     # same major


def test_fresh_mode_neither_reads_nor_writes(settings, tmp_path):
    m = SchemaMemory.open("abc", 160004, settings)
    m.put("ns", "k", 1)
    m.save()
    fresh = SchemaMemory.open("abc", 160004, settings.with_(use_schema_memory=False))
    assert fresh.get("ns", "k") is None and not fresh.persistent
    fresh.put("ns", "k", 2)
    fresh.save()
    assert SchemaMemory.open("abc", 160004, settings).get("ns", "k") == 1


def test_two_writers_merge_instead_of_overwriting(settings):
    a = SchemaMemory.open("abc", 160004, settings)
    b = SchemaMemory.open("abc", 160004, settings)
    a.put("ns", "from_a", 1)
    b.put("ns", "from_b", 2)
    a.save()
    b.save()
    m = SchemaMemory.open("abc", 160004, settings)
    assert m.namespace("ns") == {"from_a": 1, "from_b": 2}


def test_delete(settings):
    m = SchemaMemory.open("abc", 160004, settings)
    m.put("ns", "k", 1)
    m.save()
    m.delete("ns", "k")
    m.save()
    assert SchemaMemory.open("abc", 160004, settings).get("ns", "k") is None


def test_corrupt_file_is_set_aside(settings, tmp_path):
    m = SchemaMemory.open("abc", 160004, settings)
    m.put("ns", "k", 1)
    m.save()
    m.path.write_text("{ broken")
    again = SchemaMemory.open("abc", 160004, settings)
    assert again.get("ns", "k") is None
    assert list(m.path.parent.glob("*.corrupt-*"))


def test_returned_values_are_copies(settings):
    m = SchemaMemory.open("abc", 160004, settings)
    m.put("ns", "k", {"a": [1]})
    m.get("ns", "k")["a"].append(2)
    assert m.get("ns", "k") == {"a": [1]}


def test_non_json_value_fails_at_put(settings):
    m = SchemaMemory.open("abc", 160004, settings)
    m.put("ns", "k", object())          # stored as its repr, never breaks the file
    m.save()
    assert isinstance(json.loads(m.path.read_text())["facts"]["ns"]["k"], str)
