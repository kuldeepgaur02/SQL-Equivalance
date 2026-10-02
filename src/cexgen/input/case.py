"""Step 1 — Input: the Case every later step works on.

A Case is the schema text plus two query texts. It is validated once, here,
so later steps can trust it:

  - texts are non-empty, contain no NUL bytes and no psql meta-commands
  - strings, quoted identifiers, comments and dollar quotes are all terminated
  - the schema has at least one statement
  - each query is exactly one statement, stored without its trailing ';'
  - queries carry no $1-style parameters (there would be no values for them)

Whether a query is a read-only SELECT is decided in Step 3 (query parsing),
where the SQL is parsed properly.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from ..errors import InputError
from ..sqltext.lexer import (LexError, excerpt, find_backslash, find_positional_params, fingerprint, line_col,
                             split_statements)

MAX_NAME_LENGTH = 200


@dataclass(frozen=True)
class Case:
    name: str
    schema_sql: str
    q1: str
    q2: str
    source: str = "<memory>"                                   # where it came from, for messages
    meta: Mapping[str, Any] = field(default_factory=dict)      # free-form, e.g. {"expected": "different"}

    def __post_init__(self) -> None:
        name = self.name.strip() if isinstance(self.name, str) else ""
        if not name:
            raise InputError(f"{self.source}: case name is empty")
        if len(name) > MAX_NAME_LENGTH or any(ord(c) < 32 for c in name):
            raise InputError(f"{self.source}: case name must be at most {MAX_NAME_LENGTH} printable characters")
        object.__setattr__(self, "name", name)

        label = f"{self.source} [{name}]"
        _check_text(label, "schema", self.schema_sql)
        if not _statements(label, "schema", self.schema_sql):
            raise InputError(f"{label}: schema has no SQL statements (only whitespace or comments)")
        object.__setattr__(self, "q1", _single_query(label, "Q1", self.q1))
        object.__setattr__(self, "q2", _single_query(label, "Q2", self.q2))

        try:
            frozen = json.loads(json.dumps(dict(self.meta)))     # must be plain JSON data
        except (TypeError, ValueError) as e:
            raise InputError(f"{label}: meta must be JSON-serialisable ({e})") from None
        object.__setattr__(self, "meta", MappingProxyType(frozen))

    @property
    def identical_queries(self) -> bool:
        """Q1 and Q2 are the same text apart from comments, whitespace and keyword case.
        Such a pair is trivially equivalent; later steps may short-cut it."""
        return fingerprint(self.q1) == fingerprint(self.q2)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "source": self.source, "schema_sql": self.schema_sql,
                "q1": self.q1, "q2": self.q2, "meta": copy.deepcopy(dict(self.meta))}


def _check_text(label: str, what: str, text: Any) -> None:
    if not isinstance(text, str):
        raise InputError(f"{label}: {what} must be text, got {type(text).__name__}")
    if "\x00" in text:
        line, col = line_col(text, text.index("\x00"))
        raise InputError(f"{label}: {what} contains a NUL byte at line {line}, column {col}")
    if not text.strip():
        raise InputError(f"{label}: {what} is empty")
    pos = _safe_backslash(label, what, text)
    if pos is not None:
        line, col = line_col(text, pos)
        raise InputError(
            f"{label}: {what} has a backslash outside a string at line {line}, column {col}. "
            f"psql meta-commands (\\connect, \\i, \\set ...) are not SQL; remove them.\n{excerpt(text, pos)}")


def _safe_backslash(label: str, what: str, text: str) -> int | None:
    try:
        return find_backslash(text)
    except LexError as e:
        line, col = line_col(text, e.offset)
        raise InputError(f"{label}: {what}: {e} (starts at line {line}, column {col})\n{excerpt(text, e.offset)}") from None


def _statements(label: str, what: str, text: str):
    try:
        return split_statements(text)
    except LexError as e:  # pragma: no cover - _check_text lexes first
        raise InputError(f"{label}: {what}: {e}") from None


def _single_query(label: str, what: str, text: Any) -> str:
    _check_text(label, what, text)
    stmts = _statements(label, what, text)
    if not stmts:
        raise InputError(f"{label}: {what} has no SQL (only whitespace or comments)")
    if len(stmts) > 1:
        line, _ = line_col(text, stmts[1].start)
        raise InputError(f"{label}: {what} must be one statement, found {len(stmts)} "
                         f"(the second starts at line {line})")
    params = find_positional_params(text)
    if params:
        line, col = line_col(text, params[0])
        raise InputError(f"{label}: {what} uses positional parameters ($1 ...) at line {line}, column {col}; "
                         f"replace them with literal values\n{excerpt(text, params[0])}")
    return stmts[0].text
