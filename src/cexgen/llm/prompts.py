"""Every prompt cexgen sends to an LLM, in one place for review.

Values travel as JSON text in a `value_json` field: numbers as JSON numbers,
text as JSON strings, SQL NULL as null, json/jsonb as the document itself, and
SQL arrays as JSON arrays. One encoding for every type keeps the output schema
fixed, so structured output can enforce it.
"""
from __future__ import annotations

SYSTEM = """\
You help a tool that searches for a small PostgreSQL database instance on which \
two SQL queries (Q1 and Q2) return different results. You produce values for \
specific columns. Every value must be valid for its column: its type, NOT NULL, \
lengths, enum labels and CHECK rules. Encode each value as JSON text in \
`value_json`: numbers as JSON numbers, text, dates and enum labels as JSON \
strings, NULL as null, json/jsonb columns as the JSON document itself, and SQL \
arrays as JSON arrays. Use only the tables and columns you are asked about."""

# -- Step 5: JSON / ARRAY values for the base row ----------------------------------

JSON_ARRAY_SCHEMA = {
    "type": "object",
    "properties": {
        "values": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "table": {"type": "string"},
                    "column": {"type": "string"},
                    "value_json": {"type": "string"},
                },
                "required": ["table", "column", "value_json"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["values"],
    "additionalProperties": False,
}


def json_array_prompt(schema_text: str, q1: str, q2: str, requests: list[dict]) -> str:
    lines = [
        "## Schema", schema_text, "",
        "## Q1", q1, "", "## Q2", q2, "",
        "## Task",
        "Give one value for each JSON / ARRAY column below. It goes into the base row of its table "
        "(one row per table). For a JSON column, build a document in which every path the queries "
        "read exists; for an array column, give a short array of valid elements.",
        "",
    ]
    for r in requests:
        paths = ", ".join("->".join(map(str, p)) for p in r["paths"]) or "(none)"
        lines.append(f"- {r['table']}.{r['column']}: {r['type']}; paths the queries read: {paths}"
                     + (f"; element: {r['element']}" if r.get("element") else ""))
    return "\n".join(lines)


# -- Step 6: repair a row whose INSERT failed (CHECK / unknown) -------------------------

REPAIR_SCHEMA = {
    "type": "object",
    "properties": {
        "note": {"type": "string"},
        "row": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"column": {"type": "string"}, "value_json": {"type": "string"}},
                "required": ["column", "value_json"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["note", "row"],
    "additionalProperties": False,
}


def repair_prompt(table_text: str, row_json: str, error: dict, constraint_text: str | None,
                  attempts: list[dict], failed_before: list) -> str:
    lines = [
        "## Table", table_text, "",
        "## Row that failed to insert", row_json, "",
        f"## Postgres error (SQLSTATE {error.get('sqlstate')})", error.get("message", ""),
    ]
    if constraint_text:
        lines += ["", f"## Constraint {error.get('constraint')}", constraint_text]
    if attempts:
        lines += ["", "## Earlier attempts on this row, with their exact results"]
        lines += [f"- {a['row']} -> {a['result']}" for a in attempts]
    if failed_before:
        lines += ["", "## Values that failed this constraint in earlier runs (do not repeat)"]
        lines += [f"- {v}" for v in failed_before]
    lines += ["", "## Task",
              "Return the full corrected row (every column listed in the row above). Change as few values "
              "as possible, keep foreign-key columns as they are, and make the row satisfy every rule of the "
              "table. Put a one-line reason in `note`."]
    return "\n".join(lines)


# -- Step 8: rebuild the base when both queries return no rows ---------------------------

REBUILD_SCHEMA = {
    "type": "object",
    "properties": {
        "note": {"type": "string"},
        "tables": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "table": {"type": "string"},
                    "cells": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"column": {"type": "string"}, "value_json": {"type": "string"}},
                            "required": ["column", "value_json"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["table", "cells"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["note", "tables"],
    "additionalProperties": False,
}


def rebuild_prompt(schema_text: str, q1: str, q2: str, filters: list[str], rows_json: str,
                   history: list[dict]) -> str:
    lines = ["## Schema", schema_text, "", "## Q1", q1, "", "## Q2", q2]
    if filters:
        lines += ["", "## Conditions found in the queries"] + [f"- {f}" for f in filters]
    lines += ["", "## Current data (one row per table); both queries return NO rows on it", rows_json]
    for h in history:
        lines += ["", f"## Earlier rebuild {h['round']} (did not help)", h["rows"],
                  f"Result: Q1 {h['q1']}, Q2 {h['q2']}"]
    lines += ["", "## Task",
              "The data misses the queries' filters, so both return nothing. Change the values of this one row "
              "per table so that at least one query (ideally both) returns rows: make the WHERE / JOIN / HAVING "
              "conditions true, using the constants in the queries. Keep every rule of the schema (types, NOT NULL, "
              "keys, CHECK). A foreign key value must equal the key of its parent row, so change both together. "
              "List only the cells you change, per table. Do not repeat an earlier rebuild. One-line reason in `note`."]
    return "\n".join(lines)
