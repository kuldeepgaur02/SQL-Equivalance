"""Turning a Postgres error raised by the schema SQL into a SchemaError that
points at the line, shows it, and says what to do about it."""
from __future__ import annotations

import psycopg2

from ..errors import SchemaError
from ..sqltext.lexer import excerpt, line_col

# SQLSTATE -> advice, for the failures real-world schema files hit most.
_HINTS: dict[str, str] = {
    "42601": "SQL syntax error.",
    "42704": "An object the schema refers to does not exist here.",
    "42P01": "A table is used before it is created; reorder the CREATE TABLE statements.",
    "42P06": "The schema already exists (every database has 'public'); use CREATE SCHEMA IF NOT EXISTS.",
    "42P07": "An object is created twice.",
    "42710": "An object is created twice.",
    "42883": "A function or operator does not exist; is a CREATE EXTENSION or CREATE FUNCTION missing?",
    "3F000": "The schema named here is never created; add CREATE SCHEMA.",
    "42501": "Permission denied; the Postgres user needs to own the sandbox database (the bundled Docker user does).",
    "25001": "This statement cannot run inside a transaction block (e.g. CREATE INDEX CONCURRENTLY); drop CONCURRENTLY.",
    "0A000": "Feature not supported by this Postgres server.",
    "58P01": "A file the server needs is missing, usually an extension that is not installed in this Postgres image.",
    "57014": "The schema SQL ran past statement_timeout (CEX_STATEMENT_TIMEOUT_MS).",
    "55P03": "Could not get a lock in time (lock_timeout).",
    "23505": "The schema inserts duplicate rows.",
    "23503": "The schema inserts a row that violates a foreign key.",
}


def schema_error(e: psycopg2.Error, ddl: str) -> SchemaError:
    diag = e.diag
    message = (diag.message_primary or str(e)).strip()
    sqlstate = e.pgcode
    hint = diag.message_hint or _hint(sqlstate, message)
    if diag.message_detail:
        message = f"{message} ({diag.message_detail.strip()})"

    line = column = None
    snippet = None
    # statement_position is a 1-based character offset into the text we sent.
    pos = diag.statement_position
    if pos and pos.isdigit():
        offset = int(pos) - 1
        line, column = line_col(ddl, offset)
        snippet = excerpt(ddl, offset)
    elif diag.context:
        message = f"{message}\n  context: {diag.context.strip().splitlines()[0]}"
    return SchemaError(f"schema SQL failed: {message}", sqlstate=sqlstate, line=line, column=column,
                       excerpt=snippet, hint=hint)


def _hint(sqlstate: str | None, message: str) -> str | None:
    low = message.lower()
    if sqlstate == "42704" and "role" in low:
        return ("The schema references a role that does not exist here (OWNER TO / GRANT / SET ROLE). "
                "Dump with pg_dump --no-owner --no-privileges, or delete those lines.")
    if sqlstate == "42704" and low.startswith("type"):
        return ("The type is not visible: it is created later in the file, belongs to a missing extension, "
                "or lives in a schema that is not on search_path (pg_dump files set search_path to ''; "
                "qualify it, e.g. public.citext).")
    if "extension" in low and sqlstate in ("58P01", "0A000", "42704"):
        return "This extension is not available in the Postgres server; install it or remove CREATE EXTENSION."
    return _HINTS.get(sqlstate or "")
