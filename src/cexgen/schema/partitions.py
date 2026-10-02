"""Partition bounds, read from the text Postgres prints for them.

    FOR VALUES FROM ('2024-01-01') TO ('2025-01-01')     range (one value per key column)
    FOR VALUES FROM (MINVALUE) TO (10)                   open-ended range
    FOR VALUES IN ('a', 'b', NULL)                       list
    FOR VALUES WITH (modulus 4, remainder 1)             hash
    DEFAULT                                              everything no other partition takes

Logically the partitioned table is one table; its partitions' bounds add a rule
its rows must satisfy: the key must fall into some partition, unless there is
a DEFAULT partition.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..sqltext.lexer import CODE, STRING, tokenize


@dataclass(frozen=True)
class BoundValue:
    kind: str                 # "literal" | "null" | "minvalue" | "maxvalue"
    text: str | None = None   # the literal's text, without quotes ('2024-01-01' -> 2024-01-01)
    quoted: bool = False      # was a string literal (vs a bare number / true / false)

    def __str__(self) -> str:
        if self.kind != "literal":
            return self.kind.upper()
        return "'" + self.text.replace("'", "''") + "'" if self.quoted else self.text


@dataclass(frozen=True)
class PartitionBound:
    kind: str                                  # "range" | "list" | "hash" | "default"
    text: str                                  # as Postgres printed it
    lower: tuple[BoundValue, ...] = ()         # range: inclusive
    upper: tuple[BoundValue, ...] = ()         # range: exclusive
    values: tuple[BoundValue, ...] = ()        # list
    modulus: int | None = None                 # hash
    remainder: int | None = None


class BoundParseError(ValueError):
    pass


def parse_bound(text: str) -> PartitionBound:
    atoms = _atoms(text)
    words = [a[1].upper() for a in atoms if a[0] == "code"]
    if words[:1] == ["DEFAULT"] and len(atoms) == 1:
        return PartitionBound("default", text)
    if words[:2] != ["FOR", "VALUES"] or len(atoms) < 3:
        raise BoundParseError(f"unrecognised partition bound: {text}")
    rest = atoms[2:]
    head = rest[0][1].upper()
    if head == "IN":
        values, after = _paren_list(rest, 1, text)
        _expect_end(after, rest, text)
        return PartitionBound("list", text, values=tuple(_value(a, text) for a in values))
    if head == "FROM":
        lower, i = _paren_list(rest, 1, text)
        if i >= len(rest) or rest[i][1].upper() != "TO":
            raise BoundParseError(f"range bound without TO: {text}")
        upper, after = _paren_list(rest, i + 1, text)
        _expect_end(after, rest, text)
        return PartitionBound("range", text, lower=tuple(_value(a, text) for a in lower),
                              upper=tuple(_value(a, text) for a in upper))
    if head == "WITH":
        items, after = _paren_list(rest, 1, text, keep_groups=True)
        _expect_end(after, rest, text)
        found = {}
        for group in items:
            if len(group) == 2 and group[0][1].lower() in ("modulus", "remainder") and group[1][1].isdigit():
                found[group[0][1].lower()] = int(group[1][1])
        if set(found) != {"modulus", "remainder"}:
            raise BoundParseError(f"hash bound without modulus/remainder: {text}")
        return PartitionBound("hash", text, modulus=found["modulus"], remainder=found["remainder"])
    raise BoundParseError(f"unrecognised partition bound: {text}")


# -- internals -----------------------------------------------------------------
def _atoms(text: str) -> list[tuple[str, str]]:
    """('code', word or punctuation) / ('string', unescaped text)."""
    out = []
    for t in tokenize(text):
        if t.kind == STRING:
            body = t.text[t.text.index("'") + 1:-1]
            out.append(("string", body.replace("''", "'")))
        elif t.kind == CODE:
            out += [("code", p) for p in re.findall(r"[(),]|[^\s(),]+", t.text)]
        else:
            raise BoundParseError(f"unexpected token in partition bound: {t.text}")
    return out


def _paren_list(atoms, i, text, keep_groups=False):
    """Read '( a, b c, d )' starting at atoms[i]. Returns (items, index after ')')."""
    if i >= len(atoms) or atoms[i] != ("code", "("):
        raise BoundParseError(f"expected '(' in partition bound: {text}")
    groups, current, j = [], [], i + 1
    while j < len(atoms):
        a = atoms[j]
        if a == ("code", ")"):
            groups.append(current)
            break
        if a == ("code", ","):
            groups.append(current)
            current = []
        else:
            current.append(a)
        j += 1
    else:
        raise BoundParseError(f"unclosed '(' in partition bound: {text}")
    if keep_groups:
        return groups, j + 1
    for g in groups:
        if len(g) != 1:
            raise BoundParseError(f"unexpected bound value {' '.join(x[1] for x in g)!r}: {text}")
    return [g[0] for g in groups], j + 1


def _value(atom, text) -> BoundValue:
    kind, value = atom
    if kind == "string":
        return BoundValue("literal", value, quoted=True)
    word = value.upper()
    if word in ("MINVALUE", "MAXVALUE", "NULL"):
        return BoundValue(word.lower())
    return BoundValue("literal", value)              # numbers, true / false


def _expect_end(after, atoms, text):
    if after != len(atoms):
        raise BoundParseError(f"unexpected text after partition bound: {text}")
