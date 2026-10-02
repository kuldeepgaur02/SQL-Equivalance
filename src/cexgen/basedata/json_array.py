"""JSON / ARRAY values for the base rows (the diagram: "LLM, built from query paths").

One LLM call per case covers every JSON / ARRAY column. Each answer is checked
before it is used (valid JSON, every path present, array elements valid for
their type). A missing or invalid answer falls back to the rule table for that
column, and the reason is recorded.
"""
from __future__ import annotations

import json
from typing import Any

from ..baseline.rules import array_value, json_document, set_path
from ..llm import prompts
from ..llm.oracle import Answer, Oracle
from ..queries.model import ColumnRef, QueryPair
from ..schema.describe import describe as describe_schema
from ..schema.model import Column, SchemaModel
from ..schema.names import QName
from ..schema.types import Family
from .defaults import default_for_type
from .model import JSON_RULE, LLM, LLM_MEMORY
from .values import coerce, fits

NAMESPACE = "json_array"     # schema-memory namespace for cached LLM answers


def json_array_values(columns: list[tuple[QName, Column]], model: SchemaModel, queries: QueryPair | None,
                      oracle: Oracle) -> dict[tuple[QName, str], Answer]:
    requests = []
    for table, col in columns:
        ref = ColumnRef(table, col.name)
        paths = []
        for q in (queries.q1, queries.q2) if queries else ():
            for p in q.json_paths.get(ref, ()):
                if p not in paths:
                    paths.append(p)
        requests.append({"table": str(table), "column": col.name, "type": col.type.sql, "paths": paths,
                         "element": col.type.element.sql if col.type.element is not None else None,
                         "_key": (table, col.name), "_column": col})

    out: dict[tuple[QName, str], Answer] = {}
    if oracle.mode == "rules":
        for r in requests:
            out[r["_key"]] = Answer(_rule_value(r), JSON_RULE)
        return out

    pending = []
    for r in requests:
        cache_key = oracle.cache_key(r["table"], r["column"], r["type"], r["paths"])
        r["_cache"] = cache_key
        cached = oracle.cached(NAMESPACE, cache_key)
        if cached is not None:
            out[r["_key"]] = Answer(_decode_cached(cached, r["_column"]), LLM_MEMORY)
        else:
            pending.append(r)
    if not pending:
        return out

    prompt = prompts.json_array_prompt(describe_schema(model), queries.q1.sql if queries else "",
                                       queries.q2.sql if queries else "",
                                       [{k: v for k, v in r.items() if not k.startswith("_")} for r in pending])
    answer = oracle.ask("base_data", "JSON / ARRAY values for the base rows", prompt, prompts.JSON_ARRAY_SCHEMA)
    given = {}
    for item in (answer or {}).get("values", []):
        given[(item.get("table"), item.get("column"))] = item.get("value_json")

    for r in pending:
        text = given.get((r["table"], r["column"]))
        if answer is None:
            out[r["_key"]] = Answer(_rule_value(r), JSON_RULE, "LLM call failed; rule table used")
            continue
        value, problem = _accept(text, r)
        if problem:
            out[r["_key"]] = Answer(_rule_value(r), JSON_RULE, f"LLM answer rejected ({problem}); rule table used")
            continue
        out[r["_key"]] = Answer(value, LLM)
        oracle.remember(NAMESPACE, r["_cache"], value)
    return out


def _rule_value(r: dict) -> Any:
    col: Column = r["_column"]
    if col.type.family == Family.JSON:
        return json_document(r["paths"])
    element = default_for_type(col.type.element) if col.type.element is not None else "a"
    return array_value(col, element)


def _accept(text: Any, r: dict) -> tuple[Any, str | None]:
    """The LLM's value if it is valid for the column (missing JSON paths are added)."""
    col: Column = r["_column"]
    if not isinstance(text, str):
        return None, "no value given"
    try:
        value = json.loads(text)
    except ValueError:
        return None, "not valid JSON"
    if col.type.family == Family.JSON:
        if value is None and not col.nullable:
            return None, "NULL for a NOT NULL column"
        for path in r["paths"]:
            if not _has(value, path):
                value = set_path(value if isinstance(value, (dict, list)) else {}, path, "a")
        return value, None
    if not isinstance(value, list) or not value:
        return None, "not a non-empty array"
    element = col.type.element
    if element is not None:
        flat = _flatten(value)
        checked = [coerce(v, _as_column(col, element)) for v in flat]
        if not all(v is None or fits(v, _as_column(col, element)) for v in checked):
            return None, f"an element is not a valid {element.sql}"
        value = _rebuild(value, iter(checked))
    return value, None


def _decode_cached(value: Any, col: Column) -> Any:
    if col.type.family == Family.ARRAY and col.type.element is not None:
        return _rebuild(value, iter(coerce(v, _as_column(col, col.type.element)) for v in _flatten(value)))
    return value


def _as_column(col: Column, t) -> Column:
    return Column(col.name, col.position, t, True)


def _flatten(value):
    if isinstance(value, list):
        return [x for v in value for x in _flatten(v)]
    return [value]


def _rebuild(shape, values):
    if isinstance(shape, list):
        return [_rebuild(v, values) for v in shape]
    return next(values)


def _has(doc: Any, path: tuple) -> bool:
    for key in path:
        if isinstance(key, int) and isinstance(doc, list) and 0 <= key < len(doc):
            doc = doc[key]
        elif isinstance(doc, dict) and str(key) in doc:
            doc = doc[str(key)]
        else:
            return False
    return True
