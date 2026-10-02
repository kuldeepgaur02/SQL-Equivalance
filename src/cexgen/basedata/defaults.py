"""Hardcoded default values: one per type family, within the column's limits.

Deliberately ignores the queries (as the diagram says). Values are plain Python:
int, Decimal, float, bool, str (dates, times, uuids, enum labels, network,
geometric, ranges...), bytes, list (arrays), tuple (composites), dict (json).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from ..schema.model import Column
from ..schema.types import Family, TypeInfo

_GEOMETRIC = {
    "point": "(0,0)", "line": "{1,-1,0}", "lseg": "[(0,0),(1,1)]", "box": "(1,1),(0,0)",
    "path": "[(0,0),(1,1)]", "polygon": "((0,0),(1,0),(1,1))", "circle": "<(0,0),1>",
}
_NETWORK = {"inet": "192.0.2.1", "cidr": "192.0.2.0/24", "macaddr": "08:00:2b:01:02:03",
            "macaddr8": "08:00:2b:01:02:03:04:05"}
_OTHER = {"xml": "<a/>", "tsvector": "a", "tsquery": "a", "pg_lsn": "0/0", "hstore": "a=>b"}


def default_value(column: Column) -> Any:
    return default_for_type(column.type)


def default_for_type(t: TypeInfo) -> Any:
    f = t.family
    if f == Family.INTEGER:
        return max(t.min_value, min(1, t.max_value)) if t.min_value is not None else 1
    if f == Family.NUMERIC:
        return numeric_default(t)
    if f == Family.FLOAT:
        return 1.0
    if f == Family.MONEY:
        return "1.00"
    if f == Family.TEXT:
        return "a"
    if f == Family.BOOLEAN:
        return True
    if f == Family.DATE:
        return "2000-01-01"
    if f == Family.TIME:
        return "00:00:00+00" if t.with_time_zone else "00:00:00"
    if f == Family.TIMESTAMP:
        return "2000-01-01 00:00:00+00" if t.with_time_zone else "2000-01-01 00:00:00"
    if f == Family.INTERVAL:
        return "1 day"
    if f == Family.UUID:
        return "00000000-0000-0000-0000-000000000001"
    if f == Family.JSON:
        return {}
    if f == Family.BYTES:
        return b"\x00"
    if f == Family.ENUM:
        return t.enum_labels[0] if t.enum_labels else None
    if f == Family.ARRAY:
        element = default_for_type(t.element) if t.element is not None else "a"
        value: Any = [element]
        for _ in range(max(t.dimensions, 1) - 1):
            value = [value]
        return value
    if f == Family.RANGE:
        return range_default(t)
    if f == Family.COMPOSITE:
        return tuple(default_for_type(ft) for _, ft in t.fields)
    if f == Family.NETWORK:
        return _NETWORK.get(t.base, "192.0.2.1")
    if f == Family.BIT:
        n = t.length or 1
        return "1" + "0" * (n - 1) if not t.varying else "1"
    if f == Family.GEOMETRIC:
        return _GEOMETRIC.get(t.base, "(0,0)")
    return _OTHER.get(t.base, "a")


def numeric_default(t: TypeInfo) -> Decimal:
    """1, written with the column's scale; the smallest positive step if 1 does not fit."""
    if t.precision is None or t.scale is None:
        return Decimal(1)
    scale = t.scale
    if scale < 0:                                   # numeric(5,-2): multiples of 100
        return Decimal(10) ** -scale
    if t.precision - scale >= 1:
        return Decimal(1).quantize(Decimal(1).scaleb(-scale))
    return Decimal(1).scaleb(-scale)                # numeric(2,2) -> 0.01


def numeric_step(t: TypeInfo) -> Decimal:
    if t.family == Family.NUMERIC and t.scale is not None:
        return Decimal(1).scaleb(-t.scale)
    return Decimal(1)


def range_default(t: TypeInfo) -> str:
    sub = t.subtype.family if t.subtype is not None else Family.INTEGER
    if sub == Family.DATE:
        r = "[2000-01-01,2000-01-02)"
    elif sub == Family.TIMESTAMP:
        r = '["2000-01-01 00:00:00","2000-01-01 01:00:00")'
    else:
        r = "[1,2)"
    return "{" + r + "}" if t.multirange else r
