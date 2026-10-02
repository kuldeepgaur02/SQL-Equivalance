from decimal import Decimal

import pytest

from cexgen.basedata import default_for_type
from cexgen.basedata.values import compare, fits, from_like, like_matches, predecessor, successor
from cexgen.schema import Column, Family, QName, TypeInfo


def t(family, base="x", **kw):
    return TypeInfo(QName("pg_catalog", base), base, base, family, **kw)


def col(typ, nullable=True):
    return Column("c", 1, typ, nullable)


@pytest.mark.parametrize("typ, expected", [
    (t(Family.INTEGER, "int2", min_value=-32768, max_value=32767), 1),
    (t(Family.NUMERIC, "numeric", precision=10, scale=2), Decimal("1.00")),
    (t(Family.NUMERIC, "numeric", precision=2, scale=2), Decimal("0.01")),        # 1 does not fit
    (t(Family.NUMERIC, "numeric", precision=5, scale=-2), Decimal("100")),
    (t(Family.NUMERIC, "numeric"), Decimal(1)),
    (t(Family.TEXT, "varchar", length=1), "a"),
    (t(Family.BIT, "bit", length=4, varying=False), "1000"),
    (t(Family.TIMESTAMP, "timestamptz", with_time_zone=True), "2000-01-01 00:00:00+00"),
    (t(Family.ENUM, "mood", enum_labels=("sad", "ok")), "sad"),
    (t(Family.ENUM, "nothing"), None),
    (t(Family.RANGE, "daterange", subtype=t(Family.DATE, "date")), "[2000-01-01,2000-01-02)"),
    (t(Family.ARRAY, "_int4", element=t(Family.INTEGER, "int4", min_value=-1, max_value=9), dimensions=2), [[1]]),
    (t(Family.COMPOSITE, "addr", fields=(("a", t(Family.TEXT, "text")), ("b", t(Family.BOOLEAN, "bool")))),
     ("a", True)),
    (t(Family.OTHER, "xml"), "<a/>"),
])
def test_defaults_fit_their_type(typ, expected):
    value = default_for_type(typ)
    assert value == expected
    assert fits(value, col(typ))


def test_steps_respect_type():
    int2 = col(t(Family.INTEGER, "int2", min_value=-32768, max_value=32767))
    assert successor(32767, int2) is None and predecessor(-32768, int2) is None
    money = col(t(Family.NUMERIC, "numeric", precision=10, scale=2))
    assert successor(Decimal("100"), money) == Decimal("100.01")
    date = col(t(Family.DATE, "date"))
    assert successor("2024-02-28", date) == "2024-02-29" and predecessor("2024-03-01", date) == "2024-02-29"
    enum = col(t(Family.ENUM, "e", enum_labels=("a", "b")))
    assert successor("a", enum) == "b" and successor("b", enum) is None
    v3 = col(t(Family.TEXT, "varchar", length=3))
    assert successor("ab", v3) == "aba" and successor("abc", v3) == "abd"


def test_comparisons_follow_postgres():
    char5 = col(t(Family.TEXT, "bpchar", length=5))
    assert compare("a    ", "a", char5) == 0                    # char(n) ignores trailing spaces
    enum = col(t(Family.ENUM, "e", enum_labels=("low", "high")))
    assert compare("high", "low", enum) == 1                    # label order, not alphabetical
    ts = col(t(Family.TIMESTAMP, "timestamp"))
    assert compare("2024-01-01", "2024-01-01 00:00:00", ts) == 0
    assert compare("B", "a", col(t(Family.TEXT, "text"))) == -1  # C collation: byte order


def test_like_helpers():
    assert from_like("AC-%") == "AC-" and from_like("a_c\\%") == "aac%"
    assert like_matches("AC-123", "AC-%") and not like_matches("ac-1", "AC-%")
    assert like_matches("ac-1", "AC-%", case_insensitive=True)


def test_fits_limits():
    num = col(t(Family.NUMERIC, "numeric", precision=4, scale=2))
    assert fits(Decimal("99.99"), num) and not fits(Decimal("100"), num)
    assert fits(Decimal("99.994"), num) and not fits(Decimal("99.995"), num)    # rounds to the scale first
    assert not fits(None, col(t(Family.TEXT, "text"), nullable=False))
