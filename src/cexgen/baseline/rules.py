"""The rule table: what cexgen does instead of asking the LLM (--no-llm, the
baseline for the evaluation table), and the fallback when one LLM call fails.

Steps 6 and 8 add their rules here as they are built.
"""
from __future__ import annotations

from typing import Any

from ..schema.model import Column
from ..schema.types import Family

JSON_LEAF = "a"


def json_document(paths: list[tuple]) -> Any:
    """A document in which every path exists; each leaf is "a". Integer keys make arrays.
    Longer paths first, so ('a', 'b') is not overwritten by a leaf at ('a',)."""
    doc: Any = {}
    for path in sorted(paths, key=len, reverse=True):
        if not _has_path(doc, path):
            doc = set_path(doc, path, JSON_LEAF)
    return doc


def array_value(column: Column, element_value: Any) -> list:
    """A one-element array, nested to the declared number of dimensions."""
    value: Any = [element_value]
    for _ in range(max(column.type.dimensions, 1) - 1):
        value = [value]
    return value


def set_path(doc: Any, path: tuple, value: Any) -> Any:
    if not path:
        return value
    key, rest = path[0], path[1:]
    if isinstance(key, int):
        items = doc if isinstance(doc, list) else []
        while len(items) <= key:
            items.append(None)
        items[key] = set_path(items[key], rest, value)
        return items
    obj = doc if isinstance(doc, dict) else {}
    obj[str(key)] = set_path(obj.get(str(key)), rest, value)
    return obj


def _has_path(doc: Any, path: tuple) -> bool:
    for key in path:
        if isinstance(key, int) and isinstance(doc, list) and 0 <= key < len(doc):
            doc = doc[key]
        elif isinstance(doc, dict) and str(key) in doc:
            doc = doc[str(key)]
        else:
            return False
    return True


def is_json_column(column: Column) -> bool:
    return column.type.family == Family.JSON
