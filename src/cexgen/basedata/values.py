"""Comparing and stepping values the way Postgres would, per type family.

Used by the rule solver: "is age=1 >= 18?", "the value just above 100",
"does 'AC-aaa' match 'AC-%'?". Text compares in byte order (the sandbox uses C
collation); char(n) ignores trailing spaces; enums compare by label order.
"""
from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from ..schema.model import Column
from ..schema.types import Family
from .defaults import numeric_step

NUMERIC_FAMILIES = (Family.INTEGER, Family.NUMERIC, Family.FLOAT, Family.MONEY)


def coerce(value: Any, column: Column) -> Any:
    """A constant (from a query or rule) or a stored value -> this column's Python form."""
    if value is None:
        return None
    f = column.type.family
    try:
        if f == Family.INTEGER:
            d = Decimal(str(value))
            return int(d) if d == d.to_integral_value() else d
        if f == Family.NUMERIC:
            return Decimal(str(value))
        if f == Family.FLOAT:
            return float(value)
        if f == Family.BOOLEAN:
            if isinstance(value, str):
                return value.strip().lower() in ("t", "true", "y", "yes", "on", "1")
            return bool(value)
    except (InvalidOperation, ValueError, TypeError):
        return value
    if f in (Family.JSON, Family.ARRAY, Family.COMPOSITE, Family.BYTES):
        return value
    return str(value) if not isinstance(value, str) else value


def sort_key(value: Any, column: Column) -> Any:
    """Something Python can order exactly like Postgres orders this column."""
    f = column.type.family
    if f in NUMERIC_FAMILIES:
        return Decimal(str(value))
    if f == Family.ENUM:
        labels = column.type.enum_labels
        return labels.index(value) if value in labels else -1
    if f == Family.BOOLEAN:
        return bool(value)
    if f == Family.DATE:
        return _parse(value, dt.date.fromisoformat) or str(value)
    if f == Family.TIMESTAMP:
        return _parse(_tz(value), dt.datetime.fromisoformat) or str(value)
    if f == Family.TEXT:
        s = str(value)
        return s.rstrip(" ") if column.type.base == "bpchar" else s
    return str(value)


def compare(a: Any, b: Any, column: Column) -> int | None:
    try:
        ka, kb = sort_key(a, column), sort_key(b, column)
        return (ka > kb) - (ka < kb)
    except (TypeError, InvalidOperation, ValueError):
        return None


def successor(value: Any, column: Column) -> Any:
    return _step(value, column, +1)


def predecessor(value: Any, column: Column) -> Any:
    return _step(value, column, -1)


def _step(value: Any, column: Column, direction: int) -> Any:
    t = column.type
    f = t.family
    if f == Family.INTEGER:
        v = int(Decimal(str(value))) + direction
        return v if (t.min_value is None or t.min_value <= v <= t.max_value) else None
    if f in (Family.NUMERIC, Family.MONEY):
        return Decimal(str(value)) + direction * numeric_step(t)
    if f == Family.FLOAT:
        return float(value) + direction
    if f == Family.BOOLEAN:
        b = bool(value)
        return (not b) if (direction > 0) != b else None
    if f == Family.ENUM:
        labels = t.enum_labels
        i = labels.index(value) + direction if value in labels else None
        return labels[i] if i is not None and 0 <= i < len(labels) else None
    if f == Family.DATE:
        d = _parse(value, dt.date.fromisoformat)
        return (d + dt.timedelta(days=direction)).isoformat() if d else None
    if f == Family.TIMESTAMP:
        d = _parse(_tz(value), dt.datetime.fromisoformat)
        return (d + dt.timedelta(seconds=direction)).isoformat(sep=" ") if d else None
    if f == Family.TEXT:
        s = str(value)
        if direction > 0:
            if column.type.length is None or len(s) < column.type.length:
                return s + "a"
            return s[:-1] + chr(ord(s[-1]) + 1) if s else None
        return s[:-1] if s else None
    return None


def like_matches(value: Any, pattern: str, case_insensitive: bool = False) -> bool:
    return re.fullmatch(like_regex(pattern), str(value), re.S | (re.I if case_insensitive else 0)) is not None


def like_regex(pattern: str) -> str:
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\" and i + 1 < len(pattern):
            out.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        out.append(".*" if c == "%" else "." if c == "_" else re.escape(c))
        i += 1
    return "".join(out)


def from_like(pattern: str) -> str:
    """The shortest string a LIKE pattern accepts: % -> nothing, _ -> 'a'."""
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\" and i + 1 < len(pattern):
            out.append(pattern[i + 1])
            i += 2
            continue
        out.append("" if c == "%" else "a" if c == "_" else c)
        i += 1
    return "".join(out)


def fits(value: Any, column: Column) -> bool:
    """Does the value satisfy the column's own limits (NOT NULL, length, range, labels)?"""
    t = column.type
    if value is None:
        return column.nullable
    f = t.family
    if f == Family.INTEGER:
        try:
            v = Decimal(str(value))
        except InvalidOperation:
            return False
        return v == v.to_integral_value() and (t.min_value is None or t.min_value <= v <= t.max_value)
    if f == Family.NUMERIC and t.precision is not None and t.scale is not None:
        try:
            v = Decimal(str(value)).quantize(Decimal(1).scaleb(-t.scale))   # Postgres rounds to the scale
        except InvalidOperation:
            return False
        return abs(v) < Decimal(10) ** (t.precision - t.scale)
    if f == Family.TEXT and t.length is not None:
        return len(str(value).rstrip(" ") if t.base == "bpchar" else str(value)) <= t.length
    if f == Family.ENUM:
        return value in t.enum_labels
    return True


def _parse(value, parser):
    try:
        return parser(str(value))
    except ValueError:
        return None


def _tz(value: Any) -> str:
    """'2000-01-01 00:00:00+00' -> '+00:00' (Python 3.10's fromisoformat needs the minutes)."""
    s = str(value)
    return s + ":00" if re.search(r"[+-]\d\d$", s) else s
