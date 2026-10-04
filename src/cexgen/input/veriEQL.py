"""Read VeriEQL benchmark files (https://github.com/VeriEQL/VeriEQL, CC BY-NC-SA 4.0).

Each line of a `.jsonlines` file is one query pair:

    {"schema": {"EMP": {"EMPNO": "INT", ...}}, "constraint": [{"primary": [...]}, ...],
     "pair": [Q1, Q2], "index": 0, ...}

It becomes a Case with a PostgreSQL schema:

  types        INT -> integer, VARCHAR -> varchar, DATE, TIME, NUMERIC,
               BOOL / BOOLEAN -> smallint for MySQL sets (it is TINYINT(1) there), else boolean,
               "ENUM,a,b" -> varchar + CHECK (col IN ('a','b'))  (MySQL enums behave like strings;
               a NULL label means the column may be NULL)
  primary      first one per table -> PRIMARY KEY; later ones -> UNIQUE + NOT NULL (same meaning);
               several columns in one entry -> a composite key
  not_null     NOT NULL
  gt/gte/lt/lte, between, in
               CHECK, plus NOT NULL: VeriEQL's checker rejects NULL for these
  eq/neq       CHECK (NULL allowed, as in VeriEQL)
  foreign      FOREIGN KEY when the parent column is a key; otherwise recorded as not enforced
               (Postgres needs a unique parent column)
  imply / inc / consec and cross-table rules
               not expressible in Postgres DDL: recorded in meta, the case is flagged

LeetCode queries are MySQL; they are translated to PostgreSQL with sqlglot and
the originals are kept in meta. VeriEQL's own verdict for the pair (from
experiments/<date>/<set>.out) is attached as meta["veriEQL"], so our results
can be compared with theirs.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp

from ..config import Settings
from ..errors import InputError
from ..sqltext.lexer import CODE, tokenize
from ..sqltext.names import quote_ident
from .case import Case
from .files import read_text

log = logging.getLogger(__name__)

_TYPES = {"INT": "integer", "INTEGER": "integer", "VARCHAR": "varchar", "TEXT": "text", "DATE": "date",
          "TIME": "time", "BOOLEAN": "boolean", "BOOL": "boolean", "NUMERIC": "numeric", "DECIMAL": "numeric",
          "FLOAT": "double precision", "TIMESTAMP": "timestamp"}
_DOLLAR_NAME = re.compile(r"(?<![\w$])\$[A-Za-z0-9_]+")
_COMPARE = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "eq": "=", "neq": "<>"}
_NULL_FAILS = {"gt", "gte", "lt", "lte", "between", "in"}          # VeriEQL's checker rejects NULL for these
_RESULT_FILES = {"calcite2": "calcite", "calcite": "calcite", "literature": "literature",
                 "literature-rewrite": "literature", "leetcode": "leetcode"}


class VeriEQLSource:
    name = "veriEQL"

    def matches(self, path: Path) -> bool:
        if not (path.is_file() and path.suffix == ".jsonlines"):
            return False
        try:
            with open(path, encoding="utf-8") as f:
                first = json.loads(f.readline())
        except (OSError, ValueError):
            return False
        return isinstance(first, dict) and "pair" in first and "schema" in first

    def load(self, path: Path, settings: Settings) -> list[Case]:
        set_name = path.stem
        dialect = "mysql" if "leetcode" in set_name.lower() else None
        verdicts = _verdicts(path, set_name)
        cases, problems = [], []
        for n, line in enumerate(read_text(path, max(settings.max_input_bytes, path.stat().st_size)).splitlines(), 1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                # matched by the exact pair: `index` is not unique (LeetCode restarts it per problem)
                verdict = verdicts.get(json.dumps(entry.get("pair")))
                cases.append(entry_to_case(entry, set_name, dialect, verdict, str(path), n))
            except (ValueError, KeyError, TypeError, InputError) as e:
                problems.append(f"line {n}: {e}")
        if problems:
            log.warning("%s: %d of %d entries could not be turned into cases (first: %s)",
                        path.name, len(problems), len(problems) + len(cases), problems[0])
        self.problems = problems
        return cases


def entry_to_case(entry: dict, set_name: str, dialect: str | None, verdict: dict | None, source: str,
                  line: int) -> Case:
    index = entry.get("index")
    schema_sql, notes = schema_to_sql(entry["schema"], entry.get("constraint") or [], dialect)
    q1, q2 = entry["pair"]
    meta: dict[str, Any] = {"dataset": "VeriEQL", "set": set_name, "line": line, "index": index}
    for key in ("benchmark", "name", "file"):
        if entry.get(key):
            meta[key] = entry[key]
    if dialect is None:                       # Calcite: quote its generated names ($f0, $2) for Postgres
        t1, t2 = quote_dollar_names(q1), quote_dollar_names(q2)
        if (t1, t2) != (q1, q2):
            meta["original_pair"] = [q1, q2]
        q1, q2 = t1, t2
    if dialect:
        t1, t2 = translate(q1, dialect), translate(q2, dialect)
        if (t1, t2) != (q1, q2):
            meta["original_dialect"] = dialect
            meta["original_pair"] = [q1, q2]
        q1, q2 = t1, t2
    if notes:
        meta["not_enforced"] = notes                 # rules VeriEQL assumes that Postgres DDL cannot express
    if verdict:
        meta["veriEQL"] = verdict
    return Case(f"veriEQL-{set_name}-{line:05d}", schema_sql, q1, q2, source=f"{source} line {line}", meta=meta)


def translate(query: str, dialect: str) -> str:
    """MySQL -> PostgreSQL. Unparseable queries are kept as written (Postgres will judge them).

    On top of sqlglot's translation, three MySQL behaviours are kept:
      names are case-insensitive        -> lower-cased, quoted only where Postgres needs it
      SUM(a > b) counts TRUE as 1       -> SUM(CASE WHEN a > b THEN 1 ELSE 0 END) (also AVG)
      ROUND(x, n) works on any number   -> ROUND(CAST(x AS NUMERIC), n) (Postgres: numeric only)
      CONCAT(1, 2) turns numbers to text -> CAST(... AS TEXT) on each part
    """
    try:
        tree = sqlglot.parse_one(query, read=dialect)
    except sqlglot.errors.SqlglotError:
        return query
    for ident in tree.find_all(exp.Identifier):
        name = ident.this.lower()
        ident.set("this", name)
        ident.set("quoted", quote_ident(name) != name)
    for agg in tree.find_all(exp.Sum, exp.Avg):
        arg = agg.this
        inner = arg.unnest() if isinstance(arg, exp.Paren) else arg
        if isinstance(inner, (exp.Predicate, exp.Connector, exp.Not, exp.Boolean)):
            agg.set("this", exp.Case(ifs=[exp.If(this=inner.copy(), true=exp.Literal.number(1))],
                                     default=exp.Literal.number(0)))
    for cat in list(tree.find_all(exp.Concat, exp.DPipe)):        # MySQL CONCAT turns numbers into text
        parts = cat.expressions if isinstance(cat, exp.Concat) else [cat.this, cat.expression]
        cast = [p if isinstance(p, exp.Literal) and p.is_string else exp.Cast(this=p.copy(), to=exp.DataType.build("TEXT"))
                for p in parts]
        if isinstance(cat, exp.Concat):
            cat.set("expressions", cast)
        else:
            cat.set("this", cast[0])
            cat.set("expression", cast[1])
    for rnd in tree.find_all(exp.Round):
        if rnd.args.get("decimals") is not None:
            rnd.set("this", exp.Cast(this=rnd.this.copy(), to=exp.DataType.build("NUMERIC")))
    try:
        return tree.sql(dialect="postgres")
    except sqlglot.errors.SqlglotError:
        return query


def quote_dollar_names(query: str) -> str:
    """Calcite names its generated columns $f0, $2 ...; Postgres reads those only as quoted names.
    Only SQL code is touched, never strings or comments."""
    return "".join(_DOLLAR_NAME.sub(lambda m: '"' + m.group(0) + '"', t.text) if t.kind == CODE else t.text
                   for t in tokenize(query))


# -- schema ---------------------------------------------------------------------------
def schema_to_sql(schema: dict, constraints: list, dialect: str | None = None) -> tuple[str, list[str]]:
    """`dialect="mysql"`: BOOL / BOOLEAN are MySQL's TINYINT(1), so they become smallint (0 / 1);
    otherwise they are real booleans and the constants 0 / 1 in rules mean FALSE / TRUE."""
    tables = {t.upper(): {c.upper(): str(ty) for c, ty in cols.items()} for t, cols in schema.items()}
    booleans = set() if dialect == "mysql" else {(t, c) for t, cols in tables.items() for c, ty in cols.items()
                                                  if ty.upper() in ("BOOL", "BOOLEAN")}

    def lit(table: str, column: str, value: Any) -> str:
        if (table, column) in booleans and value in (0, 1) and not isinstance(value, bool):
            value = bool(value)
        return _literal(value)

    not_null: set[tuple[str, str]] = set()
    checks: dict[str, list[str]] = {t: [] for t in tables}
    keys: dict[str, list[list[str]]] = {t: [] for t in tables}
    foreign: list[tuple[str, str, str, str]] = []
    notes: list[str] = []

    for t, cols in tables.items():                    # ENUM columns become varchar + IN (...)
        for c, ty in cols.items():
            if ty.upper().startswith("ENUM,"):
                labels = [v for v in ty.split(",")[1:]]
                values = [v for v in labels if v.upper() != "NULL"]
                checks[t].append(f"{_ident(c)} IN ({', '.join(_literal(v) for v in values)})")

    for constraint in constraints:
        kind, body = next(iter(constraint.items()))
        try:
            if kind == "primary":
                cols = [_column(v, tables) for v in body]
                table = cols[0][0]
                if any(t != table for t, _ in cols):
                    raise ValueError("key columns from several tables")
                keys[table].append([c for _, c in cols])
                not_null.update(cols)
            elif kind == "not_null":
                not_null.add(_column(body, tables))
            elif kind == "foreign":
                (ct, cc), (pt, pc) = _column(body[0], tables), _column(body[1], tables)
                foreign.append((ct, cc, pt, pc))
            elif kind in _COMPARE:
                (t, c) = _column(body[0], tables)
                right = _operand(body[1], tables, t) if isinstance(body[1], dict) else lit(t, c, body[1])
                checks[t].append(f"{_ident(c)} {_COMPARE[kind]} {right}")
                if kind in _NULL_FAILS:
                    not_null.add((t, c))
            elif kind == "between":
                (t, c) = _column(body[0], tables)
                lo, hi = _operand(body[1], tables, t), _operand(body[2], tables, t)
                checks[t].append(f"{_ident(c)} BETWEEN {lo} AND {hi}")
                not_null.add((t, c))
            elif kind == "in":
                (t, c) = _column(body[0], tables)
                values = [v["literal"] if isinstance(v, dict) and "literal" in v else v for v in body[1]]
                listed = [v for v in values if v is not None]
                checks[t].append(f"{_ident(c)} IN ({', '.join(lit(t, c, v) for v in listed)})")
                if None not in values:
                    not_null.add((t, c))
            else:
                raise ValueError("Postgres DDL cannot express it")
        except (ValueError, KeyError, IndexError, TypeError) as e:
            notes.append(f"{kind}: {json.dumps(body)[:160]} ({e})")

    statements = []
    for t, cols in tables.items():
        lines = []
        for c, ty in cols.items():
            if ty.upper() in ("BOOL", "BOOLEAN"):
                base = "smallint" if dialect == "mysql" else "boolean"
            else:
                base = "varchar" if ty.upper().startswith("ENUM,") else _TYPES.get(ty.upper())
            if base is None:
                base = "text"
                notes.append(f"type {ty} of {t}.{c} unknown; created as text")
            lines.append(f"    {_ident(c)} {base}" + (" NOT NULL" if (t, c) in not_null else ""))
        for i, key in enumerate(keys[t]):
            lines.append(f"    {'PRIMARY KEY' if i == 0 else 'UNIQUE'} ({', '.join(_ident(c) for c in key)})")
        for check in checks[t]:
            lines.append(f"    CHECK ({check})")
        statements.append(f"CREATE TABLE {_ident(t)} (\n" + ",\n".join(lines) + "\n);")

    for ct, cc, pt, pc in foreign:                     # a real FK needs a key on the parent side
        if [pc] in keys[pt]:
            statements.append(f"ALTER TABLE {_ident(ct)} ADD FOREIGN KEY ({_ident(cc)}) "
                              f"REFERENCES {_ident(pt)} ({_ident(pc)});")
        else:
            notes.append(f"foreign: {ct}.{cc} -> {pt}.{pc} not enforced ({pt}.{pc} is not a key)")
    return "\n\n".join(statements) + "\n", notes


def _column(ref: Any, tables: dict) -> tuple[str, str]:
    if not (isinstance(ref, dict) and "value" in ref):
        raise ValueError(f"expected a column, got {ref!r}")
    table, _, column = ref["value"].upper().partition("__")
    if table not in tables or column not in tables[table]:
        raise ValueError(f"unknown column {ref['value']}")
    return table, column


def _operand(value: Any, tables: dict, table: str) -> str:
    if isinstance(value, dict) and "value" in value:
        t, c = _column(value, tables)
        if t != table:
            raise ValueError("compares columns of two tables")
        return _ident(c)
    if isinstance(value, dict) and "date" in value:
        return f"DATE {_literal(value['date'])}"
    if isinstance(value, dict) and "literal" in value:
        return _literal(value["literal"])
    return _literal(value)


def _literal(value: Any) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def _ident(name: str) -> str:
    return quote_ident(name.lower())


# -- VeriEQL's own verdicts -------------------------------------------------------------
def _verdicts(path: Path, set_name: str) -> dict[str, dict]:
    """JSON of the pair -> {"verdict", "states", "counterexample", "note"}, from the newest
    experiments/<date>/<set>.out."""
    result_name = _RESULT_FILES.get(set_name)
    root = next((p for p in path.parents if (p / "experiments").is_dir()), None)
    if result_name is None or root is None:
        return {}
    runs = sorted((d for d in (root / "experiments").iterdir() if (d / f"{result_name}.out").is_file()),
                  key=lambda d: d.name)
    if not runs:
        return {}
    out: dict[str, dict] = {}
    with open(runs[-1] / f"{result_name}.out", encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            states = [s for s in (r.get("states") or []) if s]
            if "NEQ" in states:
                verdict = "different"
            elif "EQU" in states:
                verdict = "equivalent_bounded"          # equivalent only for small tables, not proven
            else:
                verdict = "undecided"
            out[json.dumps(r.get("pair"))] = {"verdict": verdict, "states": states, "run": runs[-1].name,
                                   "counterexample": r.get("counterexample"),
                                   "note": (str(r.get("err"))[:200] if r.get("err") and verdict == "undecided"
                                            else None)}
    return out
