"""Values from two query results -> comparable keys, exactly as SQL compares them.

    5 (integer) = 5.0 (numeric)          numbers compare by exact value
    0.1 (float8) = 0.1 (numeric)         a float compares by its shortest exact decimal form
    'a    ' (char(5)) = 'a' (text)       char(n) padding does not count (Postgres casts it away)
    DATE '2024-01-01' = TIMESTAMP '2024-01-01 00:00'
    '5' (text) != 5 (number), true != 1  different kinds of value never match
    NULL only equals NULL                (the diagram: NULL stays NULL)
    JSON compares by content (key order ignored), arrays element by element

Each key is a (kind, value) tuple, so keys of different kinds never compare
equal and any two keys can be sorted (kind first).
"""
from __future__ import annotations

import datetime as dt
import json
import math
import uuid
from decimal import Decimal
from typing import Any

NULL = ("0-null",)
BPCHAR_OID = 1042
JSON_OIDS = {114, 3802}            # json, jsonb


def row_key(row: tuple, type_oids: tuple[int, ...]) -> tuple:
    return tuple(value_key(v, oid) for v, oid in zip(row, type_oids))


def value_key(value: Any, type_oid: int | None = None) -> tuple:
    if value is None:
        return NULL
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, (int, Decimal)):
        if isinstance(value, Decimal) and not value.is_finite():
            return ("num-special", str(value))
        return ("num", Decimal(value))
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return ("num-special", str(value))
        return ("num", Decimal(repr(value)) if value != 0 else Decimal(0))     # -0.0 == 0.0
    if isinstance(value, str):
        if type_oid in JSON_OIDS:                                             # a json scalar string
            return ("json", json.dumps(value))
        return ("text", value.rstrip(" ") if type_oid == BPCHAR_OID else value)
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None:      # the session runs in UTC, as Postgres compares timestamptz to timestamp
            value = value.astimezone(dt.timezone.utc).replace(tzinfo=None)
        return ("timestamp", value)
    if isinstance(value, dt.date):
        return ("timestamp", dt.datetime(value.year, value.month, value.day))  # date = midnight timestamp
    if isinstance(value, dt.time):
        return ("time", value.replace(tzinfo=None).isoformat())
    if isinstance(value, dt.timedelta):
        return ("interval", value)
    if isinstance(value, uuid.UUID):
        return ("uuid", str(value))
    if isinstance(value, (bytes, bytearray, memoryview)):
        return ("bytes", bytes(value).hex())
    if isinstance(value, (dict, list)) and type_oid in JSON_OIDS:
        return ("json", json.dumps(value, sort_keys=True, default=str))
    if isinstance(value, dict):
        return ("json", json.dumps(value, sort_keys=True, default=str))
    if isinstance(value, (list, tuple)):
        return ("array", tuple(value_key(v) for v in value))
    return ("other", str(value))


def display(value: Any) -> Any:
    """A JSON-friendly form of a result value, for reports."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (dt.date, dt.datetime, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "\\x" + bytes(value).hex()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [display(v) for v in value]
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return str(value)
    return value
