"""PostgreSQL identifiers: quoting for output, parsing (with case folding) for input.

Postgres folds unquoted names to lower case and keeps quoted ones exactly:
Users -> users, "Users" -> Users. Reserved words must always be quoted.
"""
from __future__ import annotations

import re

from .lexer import CODE, IDENT, tokenize

# PostgreSQL reserved key words (cannot be used as a bare table or column name).
RESERVED = frozenset("""
all analyse analyze and any array as asc asymmetric authorization binary both case cast check collate
collation column concurrently constraint create cross current_catalog current_date current_role
current_schema current_time current_timestamp current_user default deferrable desc distinct do else end
except false fetch for foreign freeze from full grant group having ilike in initially inner intersect into
is isnull join lateral leading left like limit localtime localtimestamp natural not notnull null offset on
only or order outer overlaps placing primary references returning right select session_user similar some
symmetric system_user table tablesample then to trailing true union unique user using variadic verbose
when where window with
""".split())

_PLAIN = re.compile(r"^[a-z_][a-z0-9_$]*$")


def quote_ident(name: str) -> str:
    """Quote an identifier only when Postgres needs it."""
    if _PLAIN.match(name) and name not in RESERVED:
        return name
    return '"' + name.replace('"', '""') + '"'


def parse_qualified(text: str) -> tuple[str, ...]:
    """'Public."My Table"' -> ('public', 'My Table'). Raises ValueError if malformed."""
    parts: list[str] = []
    expect_name = True
    for tok in tokenize(text.strip()):
        if tok.kind == IDENT:
            if not expect_name:
                raise ValueError(f"malformed name {text!r}")
            parts.append(tok.text[1:-1].replace('""', '"'))
            expect_name = False
        elif tok.kind == CODE:
            for piece in re.findall(r"\.|[^.\s]+|\s+", tok.text):
                if piece.isspace():
                    continue
                if piece == ".":
                    if expect_name:
                        raise ValueError(f"malformed name {text!r}")
                    expect_name = True
                else:
                    if not expect_name:
                        raise ValueError(f"malformed name {text!r}")
                    parts.append(piece.lower())
                    expect_name = False
        else:
            raise ValueError(f"malformed name {text!r}")
    if not parts or expect_name:
        raise ValueError(f"malformed name {text!r}")
    return tuple(parts)
