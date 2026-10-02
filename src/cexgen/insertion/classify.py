"""Which branch of the repair loop an INSERT error goes to (by Postgres SQLSTATE).

    PK / UNIQUE, FK, NOT NULL      -> fixed by code
    CHECK and everything else      -> the LLM (the rule table with --no-llm)
"""
from __future__ import annotations

import psycopg2

UNIQUE = "unique"            # 23505 (primary key or unique)
FOREIGN_KEY = "foreign_key"  # 23503
NOT_NULL = "not_null"        # 23502
CHECK = "check"              # 23514
PARTITION = "partition"      # 23514 "no partition of relation ... found for row"
EXCLUSION = "exclusion"      # 23P01
DATA = "data"                # 22xxx: too long, out of range, bad format
TRIGGER = "trigger"          # P0001: a trigger raised an exception
UNKNOWN = "unknown"

BY_CODE = (UNIQUE, FOREIGN_KEY, NOT_NULL)


def classify(e: psycopg2.Error) -> str:
    code = e.pgcode or ""
    if code == "23505":
        return UNIQUE
    if code == "23503":
        return FOREIGN_KEY
    if code == "23502":
        return NOT_NULL
    if code == "23514":
        return PARTITION if "no partition of relation" in (e.pgerror or "") else CHECK
    if code == "23P01":
        return EXCLUSION
    if code.startswith("22"):
        return DATA
    if code == "P0001":
        return TRIGGER
    return UNKNOWN
