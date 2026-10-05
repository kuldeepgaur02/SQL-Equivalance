"""PostgreSQL lexical scanning: just enough to tell code from strings and comments.

Splitting on ';' or looking for '--' is wrong as soon as a string, a quoted
identifier or a function body contains them. This scanner follows the
PostgreSQL lexical rules that matter for that:

  'standard string'      '' is an escaped quote; backslash is literal
  E'escape string'       backslash escapes the next character
  "quoted identifier"    "" is an escaped quote
  $tag$ ... $tag$        dollar quoting, tag optional ($$ ... $$)
  -- comment             to end of line
  /* comment */          nests: /* a /* b */ c */ is one comment
  $1                     positional parameter (not a dollar quote)
"""
from __future__ import annotations

import re
from dataclasses import dataclass

CODE, STRING, IDENT, DOLLAR, COMMENT, SEMICOLON = "code", "string", "ident", "dollar", "comment", "semicolon"

# A dollar-quote tag follows identifier rules but cannot contain '$'.
_DOLLAR_TAG = re.compile(r"\$(?:[A-Za-z_\u0080-￿][A-Za-z0-9_\u0080-￿]*)?\$")
_POSITIONAL = re.compile(r"\$\d+")


class LexError(ValueError):
    def __init__(self, message: str, offset: int):
        super().__init__(message)
        self.offset = offset


@dataclass(frozen=True)
class Token:
    kind: str
    text: str
    start: int

    @property
    def end(self) -> int:
        return self.start + len(self.text)


@dataclass(frozen=True)
class Statement:
    text: str         # without the terminating ';', surrounding whitespace stripped
    start: int        # offset of the first character of `text` in the source
    tokens: tuple[Token, ...]


def tokenize(sql: str) -> list[Token]:
    tokens: list[Token] = []
    n = len(sql)
    i = 0
    code_start = 0

    def flush(upto: int) -> None:
        if upto > code_start:
            tokens.append(Token(CODE, sql[code_start:upto], code_start))

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        if ch == "-" and nxt == "-":
            flush(i)
            end = sql.find("\n", i)
            end = n if end == -1 else end
            tokens.append(Token(COMMENT, sql[i:end], i))
            i = code_start = end
            continue

        if ch == "/" and nxt == "*":
            flush(i)
            depth, j = 1, i + 2
            while j < n and depth:
                if sql.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif sql.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            if depth:
                raise LexError("unterminated /* comment", i)
            tokens.append(Token(COMMENT, sql[i:j], i))
            i = code_start = j
            continue

        if ch == "'":
            # E'...' / e'...' when the E is a standalone prefix, not the end of a word like "type'"
            escape = i > 0 and sql[i - 1] in "Ee" and (i < 2 or not _is_ident_char(sql[i - 2]))
            start = i - 1 if escape else i
            flush(start)
            j = i + 1
            while True:
                if j >= n:
                    raise LexError("unterminated string literal", start)
                c = sql[j]
                if escape and c == "\\":
                    j += 2
                    continue
                if c == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            tokens.append(Token(STRING, sql[start:j + 1], start))
            i = code_start = j + 1
            continue

        if ch == '"':
            flush(i)
            j = i + 1
            while True:
                if j >= n:
                    raise LexError("unterminated quoted identifier", i)
                if sql[j] == '"':
                    if j + 1 < n and sql[j + 1] == '"':
                        j += 2
                        continue
                    break
                j += 1
            tokens.append(Token(IDENT, sql[i:j + 1], i))
            i = code_start = j + 1
            continue

        if ch == "$" and not (i > 0 and _is_ident_char(sql[i - 1])):
            m = _DOLLAR_TAG.match(sql, i)
            if m:
                tag = m.group(0)
                close = sql.find(tag, m.end())
                if close == -1:
                    raise LexError(f"unterminated dollar-quoted string {tag}", i)
                flush(i)
                end = close + len(tag)
                tokens.append(Token(DOLLAR, sql[i:end], i))
                i = code_start = end
                continue

        if ch == ";":
            flush(i)
            tokens.append(Token(SEMICOLON, ";", i))
            i = code_start = i + 1
            continue

        i += 1

    flush(n)
    return tokens


def split_statements(sql: str) -> list[Statement]:
    """Top-level statements, skipping ones that are only whitespace / comments."""
    out: list[Statement] = []
    current: list[Token] = []
    for tok in tokenize(sql) + [Token(SEMICOLON, "", len(sql))]:
        if tok.kind != SEMICOLON:
            current.append(tok)
            continue
        if any(t.kind != COMMENT and t.text.strip() for t in current):
            start, end = current[0].start, current[-1].end
            raw = sql[start:end]
            lead = len(raw) - len(raw.lstrip())
            out.append(Statement(raw.strip(), start + lead, tuple(current)))
        current = []
    return out


def fingerprint(sql: str) -> str:
    """Comments dropped, code whitespace collapsed, code lower-cased; strings and
    quoted identifiers kept exactly. Two queries with equal fingerprints are the
    same query text."""
    items: list[str] = []
    for t in tokenize(sql):
        if t.kind == CODE:
            items.extend(t.text.lower().split())
        elif t.kind != COMMENT:
            items.append(t.text)          # strings / identifiers / dollar bodies stay byte-exact
    while items and items[-1] == ";":
        items.pop()
    return " ".join(items)


def find_backslash(sql: str) -> int | None:
    """Offset of a backslash in code. Outside strings PostgreSQL has no use for
    '\\', so it is a psql meta-command (\\connect, \\i, \\set ...) or a typo."""
    for t in tokenize(sql):
        if t.kind == CODE:
            k = t.text.find("\\")
            if k != -1:
                return t.start + k
    return None


def find_positional_params(sql: str) -> list[int]:
    """Offsets of $1, $2 ... placeholders in code."""
    return [t.start + m.start() for t in tokenize(sql) if t.kind == CODE for m in _POSITIONAL.finditer(t.text)
            if not (t.start + m.start() > 0 and _is_ident_char(sql[t.start + m.start() - 1]))]


def line_col(text: str, offset: int) -> tuple[int, int]:
    """1-based (line, column) of a 0-based character offset."""
    offset = max(0, min(offset, len(text)))
    line = text.count("\n", 0, offset) + 1
    col = offset - (text.rfind("\n", 0, offset) + 1) + 1
    return line, col


def excerpt(text: str, offset: int, context: int = 1) -> str:
    """The lines around `offset`, numbered, with a caret under the column."""
    line, col = line_col(text, offset)
    lines = text.splitlines() or [""]
    first, last = max(1, line - context), min(len(lines), line)
    width = len(str(last))
    out = [f"  {n:>{width}} | {lines[n - 1]}" for n in range(first, last + 1)]
    out.append(f"  {' ' * width} | {' ' * (col - 1)}^")
    return "\n".join(out)


def _is_ident_char(c: str) -> bool:
    return c.isalnum() or c in "_$"


def replace_word(sql: str, word: str, replacement: str) -> str:
    """Replace a bare word in code only (not inside strings, quoted names or comments),
    case-insensitively. Used to turn a domain CHECK's VALUE into a column name."""
    pattern = re.compile(rf"(?<![\w$]){re.escape(word)}(?![\w$])", re.IGNORECASE)
    return "".join(pattern.sub(lambda _: replacement, t.text) if t.kind == CODE else t.text for t in tokenize(sql))
