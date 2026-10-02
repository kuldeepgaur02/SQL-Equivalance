import pytest

from cexgen.schema import BoundValue, parse_bound
from cexgen.schema.partitions import BoundParseError


def test_range():
    b = parse_bound("FOR VALUES FROM ('2024-01-01', 5) TO ('2025-01-01', MAXVALUE)")
    assert b.kind == "range"
    assert b.lower == (BoundValue("literal", "2024-01-01", True), BoundValue("literal", "5"))
    assert b.upper[1] == BoundValue("maxvalue")


def test_list_with_null_and_quotes():
    b = parse_bound("FOR VALUES IN ('it''s', NULL, 'a,b')")
    assert [v.text for v in b.values] == ["it's", None, "a,b"]
    assert b.values[1].kind == "null"


def test_hash_and_default():
    assert parse_bound("FOR VALUES WITH (modulus 4, remainder 3)").modulus == 4
    assert parse_bound("DEFAULT").kind == "default"


@pytest.mark.parametrize("text", ["FOR VALUES", "FOR VALUES FROM (1)", "FOR VALUES IN (1", "WHATEVER",
                                  "FOR VALUES WITH (modulus 4)", "FOR VALUES IN (1) extra"])
def test_malformed(text):
    with pytest.raises(BoundParseError):
        parse_bound(text)


def test_bound_value_prints_back_as_sql():
    assert str(BoundValue("literal", "it's", True)) == "'it''s'"
    assert str(BoundValue("minvalue")) == "MINVALUE"
