"""Error types shared by every step.

Each one carries a message written for the person running the tool. The CLI
maps each type to its own exit code.
"""
from __future__ import annotations


class CexError(Exception):
    """Base class: anything cexgen reports to the user on purpose."""

    exit_code = 1


class InputError(CexError):
    """The case (schema / queries / batch file) is missing or malformed."""

    exit_code = 2


class SchemaError(CexError):
    """The schema SQL did not execute in Postgres."""

    exit_code = 3

    def __init__(self, message: str, *, sqlstate: str | None = None, line: int | None = None,
                 column: int | None = None, excerpt: str | None = None, hint: str | None = None):
        self.sqlstate, self.line, self.column, self.excerpt, self.hint = sqlstate, line, column, excerpt, hint
        parts = [message]
        if line is not None:
            parts.append(f"  at line {line}, column {column}")
        if excerpt:
            parts.append(excerpt)
        if hint:
            parts.append(f"  hint: {hint}")
        super().__init__("\n".join(parts))
        self.message = message


class QueryError(SchemaError):
    """Q1 or Q2 is not a valid read-only query for this schema."""

    exit_code = 6


class DatabaseUnavailable(CexError):
    """Postgres cannot be reached, or refuses the login."""

    exit_code = 4


class SandboxError(CexError):
    """The sandbox database could not be created, used or dropped."""

    exit_code = 5


class LLMUnavailable(CexError):
    """LLM mode is on but the LLM cannot be used at all (no credentials, unknown provider).

    The run stops instead of silently using the rule table, so an "LLM" result is
    never secretly a baseline result. Use --no-llm for the baseline."""

    exit_code = 7
