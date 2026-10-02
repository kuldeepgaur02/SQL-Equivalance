"""Readable form of the base data."""
from __future__ import annotations

import json

from ..journal.entry import to_jsonable
from ..sqltext.names import quote_ident
from .model import BaseData


def describe_base(base: BaseData) -> str:
    lines = ["base data (one row per table)"]
    for table, row in base.rows.items():
        lines.append(f"  {table}")
        for col, cell in row.items():
            value = json.dumps(to_jsonable(cell.value), ensure_ascii=False)
            note = f"  ({cell.note})" if cell.note else ""
            lines.append(f"    {quote_ident(col):18} = {value:32} [{cell.source}]{note}")
    for u in base.updates:
        sets = ", ".join(f"{c} = {json.dumps(to_jsonable(v))}" for c, v in u.values.items())
        lines.append(f"  after all inserts: UPDATE {u.table} SET {sets}  ({u.fk.name})")
    for p in base.unsatisfied:
        lines.append(f"  left to repair: {p}")
    for w in base.warnings:
        lines.append(f"  warning: {w}")
    return "\n".join(lines)
