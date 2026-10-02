"""The diagram's "CHECK / unknown -> LLM" branch.

The LLM sees the table, the row, Postgres's exact error and constraint text,
every earlier attempt on this row with its exact result, and values that
failed this constraint in earlier runs (schema memory). Its answer is checked
before use: only this table's insertable columns, values that fit their type.
"""
from __future__ import annotations

import json
from typing import Any

from ..basedata.values import coerce, fits
from ..journal.entry import to_jsonable
from ..llm import prompts
from ..llm.oracle import Oracle
from ..schema.describe import describe_table
from ..schema.model import Table


def llm_fix(oracle: Oracle, table: Table, row: dict, error: dict, attempts: list[dict],
            failed_before: list) -> tuple[dict | None, str]:
    prompt = prompts.repair_prompt(
        "\n".join(describe_table(table)), json.dumps(to_jsonable(row), ensure_ascii=False), error,
        constraint_text(table, error.get("constraint")), attempts, failed_before)
    answer = oracle.ask("repair", f"repair {table.qname} ({error.get('sqlstate')} {error.get('constraint') or ''})".strip(),
                        prompt, prompts.REPAIR_SCHEMA)
    if answer is None:
        return None, "LLM call failed"
    new = dict(row)
    for cell in answer.get("row", []):
        name = cell.get("column")
        column = table.columns.get(name)
        if column is None or not column.insertable or name not in row:
            continue
        try:
            value: Any = json.loads(cell.get("value_json", ""))
        except ValueError:
            value = cell.get("value_json")
        value = coerce(value, column)
        if not fits(value, column):
            return None, f"LLM answer rejected: {value!r} does not fit {name} ({column.type.sql})"
        new[name] = value
    if new == row:
        return None, "LLM returned the row unchanged"
    return new, str(answer.get("note", ""))[:200]


def constraint_text(table: Table, name: str | None) -> str | None:
    if not name:
        return None
    for c in table.all_checks:
        if c.name == name:
            return f"CHECK ({c.expression})"
    for u in table.unique_keys:
        if u.name == name:
            return f"UNIQUE ({', '.join(u.elements)})" + (f" WHERE {u.predicate}" if u.predicate else "")
    for x in table.exclusions:
        if x.name == name:
            return x.definition
    if table.partitioning is not None:
        return f"PARTITION BY {table.partitioning.key_sql}: " + "; ".join(
            f"{p.table} {p.bound.text}" for p in table.partitioning.partitions)
    return None
