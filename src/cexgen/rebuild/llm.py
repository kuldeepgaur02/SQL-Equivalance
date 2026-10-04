"""The diagram's "LLM: rebuild base": the LLM rewrites the base rows to hit the queries' filters.

It sees the schema, both queries, the conditions Step 3 found, the current rows,
and every earlier rebuild with its exact result. Its answer is checked before
use: known tables and insertable columns only, values that fit their type.
"""
from __future__ import annotations

import json
from typing import Any

from ..basedata.model import Cell
from ..basedata.values import coerce, fits
from ..journal.entry import to_jsonable
from ..llm import prompts
from ..llm.oracle import Oracle
from ..queries.describe import describe_predicate
from ..queries.model import QueryPair
from ..schema.describe import describe as describe_schema
from ..schema.model import SchemaModel
from .rows import REBUILD_LLM, Rows, propagate_fk_values, values

NAMESPACE = "rebuild"           # schema-memory cache of answers that worked


def llm_rebuild(oracle: Oracle, model: SchemaModel, plan, queries: QueryPair, rows: Rows,
                history: list[dict], round_no: int) -> tuple[Rows | None, list[str], str]:
    """Returns (new rows or None if the call failed / gave nothing usable, what changed, note)."""
    rows_json = json.dumps(to_jsonable(values(rows)), ensure_ascii=False)
    key = oracle.cache_key(queries.q1.sql, queries.q2.sql, round_no, rows_json)
    answer = oracle.cached(NAMESPACE, key)
    from_cache = answer is not None
    if answer is None:
        filters = [f"{q.label}: {describe_predicate(p)}{'' if p.required else ' (inside OR / optional)'} [{p.scope}]"
                   for q in (queries.q1, queries.q2) for p in q.predicates]
        filters += [f"{q.label}: {u}" for q in (queries.q1, queries.q2) for u in q.unparsed]
        filters += [f"{q.label}: join {c.sql}" for q in (queries.q1, queries.q2) for c in q.comparisons]
        prompt = prompts.rebuild_prompt(describe_schema(model), queries.q1.sql, queries.q2.sql, filters, rows_json,
                                        history)
        answer = oracle.ask("rebuild", f"rebuild the base (round {round_no})", prompt, prompts.REBUILD_SCHEMA)
        if answer is None:
            return None, [], "LLM call failed"

    new = {t: dict(r) for t, r in rows.items()}
    changed: set = set()
    problems: list[str] = []
    for entry in answer.get("tables", []):
        table = model.resolve(entry.get("table", "")) if entry.get("table") else None
        if table is None or table not in new:
            problems.append(f"unknown or unplanned table {entry.get('table')!r}")
            continue
        columns = model.tables[table].columns
        for cell in entry.get("cells", []):
            name = cell.get("column")
            column = columns.get(name)
            if column is None or not column.insertable or name not in new[table]:
                problems.append(f"{table}: unknown column {name!r}")
                continue
            try:
                value: Any = json.loads(cell.get("value_json", ""))
            except ValueError:
                value = cell.get("value_json")
            value = coerce(value, column)
            if not fits(value, column):
                problems.append(f"{table}.{name}: {value!r} does not fit {column.type.sql}")
                continue
            if value != new[table][name].value:
                new[table][name] = Cell(value, REBUILD_LLM, str(answer.get("note", ""))[:120])
                changed.add((table, name))
    if not changed:
        return None, [], "the LLM changed nothing usable" + (f" ({'; '.join(problems[:3])})" if problems else "")
    propagate_fk_values(model, plan, new, changed)
    if not from_cache:
        oracle.remember(NAMESPACE, key, answer)
    described = [f"{t}.{c} = {new[t][c].value!r}" for t, c in sorted(changed)]
    note = str(answer.get("note", ""))[:200] + (f" (ignored: {'; '.join(problems[:3])})" if problems else "")
    return new, described, note
