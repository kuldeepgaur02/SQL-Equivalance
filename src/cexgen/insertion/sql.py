"""INSERT / UPDATE statements and Python -> Postgres values.

Values are sent WITHOUT explicit casts. An explicit cast such as
'abcdef'::varchar(3) silently truncates (and bit(n) casts pad or cut), so a
"successful" insert could store something else. Untyped literals go through
Postgres's assignment rules instead, which raise "value too long" etc. as a
real insert would. Arrays and composites are sent as Postgres literal text
for the same reason: an enum[] or a domain[] column then accepts them.
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import psycopg2
from psycopg2 import sql

from ..schema.model import Column, Table
from ..schema.types import Family


def insert_statement(table: Table, columns: list[Column]) -> sql.Composed:
    target = _qualified(table)
    if not columns:
        return sql.SQL("INSERT INTO {} DEFAULT VALUES RETURNING tableoid, ctid, *").format(target)
    overriding = any(c.identity == "always" for c in columns)
    return sql.SQL("INSERT INTO {} ({}) {}VALUES ({}) RETURNING tableoid, ctid, *").format(
        target,
        sql.SQL(", ").join(sql.Identifier(c.name) for c in columns),
        sql.SQL("OVERRIDING SYSTEM VALUE ") if overriding else sql.SQL(""),
        sql.SQL(", ").join(sql.Placeholder() for _ in columns),
    )


def update_statement(table: Table, columns: list[str]) -> sql.Composed:
    """UPDATE one row identified by (tableoid, ctid): works for tables without a key,
    and for partitioned tables (ctid alone is only unique within one partition)."""
    return sql.SQL("UPDATE {} SET {} WHERE tableoid = %s AND ctid = %s RETURNING tableoid, ctid, *").format(
        _qualified(table), sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(c)) for c in columns))


def to_db(value: Any, column: Column) -> Any:
    """A Python value -> a psycopg2 parameter for this column."""
    if value is None:
        return None
    f = column.type.family
    if f == Family.JSON:
        return json.dumps(value, default=str)
    if f == Family.BYTES and isinstance(value, (bytes, bytearray, memoryview)):
        return psycopg2.Binary(bytes(value))
    if f == Family.ARRAY:
        return array_literal(value, column.type.element.family if column.type.element else None)
    if f == Family.COMPOSITE and isinstance(value, (tuple, list)):
        return record_literal(value)
    if isinstance(value, float) and value != value:
        return "NaN"
    return value


def array_literal(value: Any, element_family: str | None) -> str:
    """[1, None, 'a b'] -> '{1,NULL,"a b"}' (nested lists -> nested braces)."""
    if not isinstance(value, (list, tuple)):
        return str(value)
    parts = []
    for v in value:
        if isinstance(v, (list, tuple)) and element_family != Family.COMPOSITE:
            parts.append(array_literal(v, element_family))
        elif v is None:
            parts.append("NULL")
        else:
            if element_family == Family.JSON:
                v = json.dumps(v, default=str)
            elif isinstance(v, bool):
                v = "true" if v else "false"
            elif isinstance(v, (tuple, list)):
                v = record_literal(v)
            text = str(v)
            parts.append('"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"')
    return "{" + ",".join(parts) + "}"


def record_literal(value: tuple | list) -> str:
    """('a', None, 3) -> '("a",,"3")' (an empty field is NULL)."""
    parts = []
    for v in value:
        if v is None:
            parts.append("")
        else:
            if isinstance(v, bool):
                v = "t" if v else "f"
            text = str(v) if not isinstance(v, Decimal) else format(v, "f")
            parts.append('"' + text.replace("\\", "\\\\").replace('"', '""') + '"')
    return "(" + ",".join(parts) + ")"


def _qualified(table: Table) -> sql.Composed:
    return sql.SQL("{}.{}").format(sql.Identifier(table.qname.schema), sql.Identifier(table.qname.name))
