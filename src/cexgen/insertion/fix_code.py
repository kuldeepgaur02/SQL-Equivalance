"""The diagram's "fix by code" branch: PK / UNIQUE, FK and NOT NULL failures.

Each fix asks the database for what it needs (the highest existing key, an
existing parent row), so it also works around rows the schema SQL seeded.
Returns the changed row, or None when code cannot fix it.
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from psycopg2 import sql

from ..basedata.defaults import default_value
from ..basedata.values import fits, successor
from ..ordering.planner import root_table
from ..schema.model import Column, ForeignKey, SchemaModel, Table
from ..schema.types import Family


def fix_unique(cur, table: Table, row: dict, constraint: str | None, attempt: int) -> dict | None:
    key = _unique_columns(table, constraint)
    fk_cols = {c for fk in table.foreign_keys for c in fk.columns}
    free = [c for c in key if c not in fk_cols and c in row]
    if not free:
        return None                         # a key made only of FK columns: code cannot pick other parents
    new = dict(row)
    for name in free:
        value = new_value(cur, table, table.columns[name], row[name], attempt)
        if value is None:
            return None
        new[name] = value
    return new


def fix_foreign_key(cur, model: SchemaModel, table: Table, row: dict, constraint: str | None) -> dict | None:
    fk = next((f for f in table.foreign_keys if f.name == constraint), None)
    if fk is None:
        return None
    new = dict(row)
    parent = existing_parent(cur, model, fk)
    if parent is not None:
        new.update(zip(fk.columns, parent))
        return new
    if table.fk_can_be_null(fk):
        for c in fk.columns:
            if table.columns[c].nullable:
                new[c] = None
        return new
    return None


def fix_not_null(cur, model: SchemaModel, table: Table, row: dict, column: str | None) -> dict | None:
    if column not in table.columns:
        return None
    new = dict(row)
    fk = next((f for f in table.foreign_keys if column in f.columns), None)
    if fk is not None:
        parent = existing_parent(cur, model, fk)
        if parent is None:
            return None
        new.update(zip(fk.columns, parent))
        return new
    value = default_value(table.columns[column])
    if value is None:
        return None
    new[column] = value
    return new


def existing_parent(cur, model: SchemaModel, fk: ForeignKey) -> tuple | None:
    """The referenced key of some row already in the parent table (seed rows count)."""
    parent = model.tables[root_table(model, fk.ref_table)]
    cur.execute(sql.SQL("SELECT {} FROM {}.{} WHERE {} ORDER BY ctid LIMIT 1").format(
        sql.SQL(", ").join(sql.Identifier(c) for c in fk.ref_columns),
        sql.Identifier(parent.qname.schema), sql.Identifier(parent.qname.name),
        sql.SQL(" AND ").join(sql.SQL("{} IS NOT NULL").format(sql.Identifier(c)) for c in fk.ref_columns)))
    found = cur.fetchone()
    return tuple(found) if found else None


def new_value(cur, table: Table, column: Column, current: Any, attempt: int) -> Any:
    """A value of the same type that is not yet used in this column."""
    t = column.type
    f = t.family
    if f in (Family.INTEGER, Family.NUMERIC, Family.FLOAT):
        cur.execute(sql.SQL("SELECT max({}) FROM {}.{}").format(
            sql.Identifier(column.name), sql.Identifier(table.qname.schema), sql.Identifier(table.qname.name)))
        top = cur.fetchone()[0]
        base = Decimal(str(top if top is not None else current if current is not None else 0))
        candidate = base + attempt
        if f == Family.INTEGER:
            candidate = int(candidate)
        return candidate if fits(candidate, column) else None
    if f == Family.TEXT:
        text = f"{current if current is not None else 'a'}{attempt}"
        if t.length is not None and len(text) > t.length:
            text = text[-t.length:]
        return text
    if f == Family.UUID:
        return f"00000000-0000-0000-{attempt:04x}-{(attempt * 7919) % (16 ** 12):012x}"
    if f == Family.ENUM and t.enum_labels:
        i = t.enum_labels.index(current) if current in t.enum_labels else -1
        return t.enum_labels[(i + attempt) % len(t.enum_labels)]
    if f in (Family.DATE, Family.TIMESTAMP):
        value = current
        for _ in range(attempt):
            value = successor(value, column) if value is not None else None
        return value
    if f == Family.BOOLEAN:
        return not current if current is not None else True
    return None


def _unique_columns(table: Table, constraint: str | None) -> list[str]:
    if table.primary_key and table.primary_key.name == constraint:
        return list(table.primary_key.columns)
    key = next((u for u in table.unique_keys if u.name == constraint), None)
    if key is None:
        return []
    if key.columns and not key.has_expressions:
        return list(key.columns)
    # expression index (lower(email)): the columns its expressions mention
    words = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", " ".join(key.elements)))
    return [c for c in table.columns if c in words]
