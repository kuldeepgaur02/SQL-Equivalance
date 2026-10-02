"""Step 2 — Parse schema (Postgres): read the catalog and build the SchemaModel.

Postgres is the parser: the schema SQL has already run, so the catalog holds
exactly what Postgres understood: types resolved, domains unwrapped, constraint
text normalised. All reads happen in one REPEATABLE READ, READ ONLY
transaction, so they see one consistent snapshot.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import replace

from ..db.inventory import Inventory
from ..sqltext.lexer import CODE, tokenize
from . import catalog as q
from .model import (CheckConstraint, Column, ExclusionConstraint, ForeignKey, Partition, Partitioning, PrimaryKey,
                    SchemaModel, Table, Trigger, UniqueKey, View)
from .names import QName
from .partitions import BoundParseError, PartitionBound, parse_bound
from .types import DomainCheck, Family, TypeInfo, decode_typmod, family_of

_TABLE_KINDS = {"r": "table", "p": "partitioned table", "f": "foreign table"}
_VIEW_KINDS = {"v": "view", "m": "materialized view"}
_MATCH = {"s": "simple", "f": "full", "p": "partial"}
_ACTION = {"a": "no action", "r": "restrict", "c": "cascade", "n": "set null", "d": "set default"}
_STRATEGY = {"r": "range", "l": "list", "h": "hash"}
_IDENTITY = {"a": "always", "d": "by default"}


def read_schema(conn, inventory: Inventory | None = None) -> SchemaModel:
    conn.rollback()
    try:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            return _Reader(cur, conn.server_version, inventory).read()
    finally:
        conn.rollback()


class _Reader:
    def __init__(self, cur, server_version: int, inventory: Inventory | None):
        self.cur = cur
        self.version = server_version
        self.inventory = inventory
        self.types = _TypeResolver(cur, server_version)
        self.warnings: list[str] = []

    def fetch(self, sql: str, *params) -> list[tuple]:
        self.cur.execute(sql, params)
        return self.cur.fetchall()

    # -------------------------------------------------------------------------
    def read(self) -> SchemaModel:
        search_path = tuple(self.fetch(q.SEARCH_PATH)[0][0])
        rels = {}
        for oid, nsp, name, kind, is_partition, unlogged, rls, force in self.fetch(q.RELATIONS):
            rels[oid] = dict(qname=QName(nsp, name), relkind=kind, is_partition=is_partition,
                             unlogged=unlogged, rls=rls, force=force)
        oids = list(rels)
        qname = {oid: r["qname"] for oid, r in rels.items()}

        attnames: dict[int, dict[int, str]] = defaultdict(dict)
        columns: dict[int, list[Column]] = defaultdict(list)
        for (rel, attnum, attname, typid, typmod, ndims, notnull, identity, generated, expr,
             collation, inherited) in self.fetch(q.COLUMNS, oids):
            typ = self.types.resolve(typid, typmod, ndims)
            attnames[rel][attnum] = attname
            columns[rel].append(Column(
                name=attname, position=attnum, type=typ, nullable=not (notnull or typ.domain_not_null),
                default=None if generated else strip_parens(expr), identity=_IDENTITY.get(identity),
                generated=strip_parens(expr) if generated else None, collation=collation, inherited=inherited))

        def names(rel: int, nums) -> tuple[str, ...]:
            return tuple(attnames[rel][n] for n in (nums or []))

        indexes = self._indexes(oids, attnames)
        excl_ops = dict(self.fetch(q.EXCLUSION_OPERATORS, oids)) if oids else {}

        pk: dict[int, PrimaryKey] = {}
        uniques: dict[int, list[UniqueKey]] = defaultdict(list)
        fks: dict[int, list[ForeignKey]] = defaultdict(list)
        checks: dict[int, list[CheckConstraint]] = defaultdict(list)
        exclusions: dict[int, list[ExclusionConstraint]] = defaultdict(list)
        for (con, name, kind, rel, key, frel, fkey, deferrable, deferred, validated, match, upd, dele,
             noinherit, indid, definition) in self.fetch(q.CONSTRAINTS, oids):
            cols = names(rel, key)
            if kind == "p":
                pk[rel] = PrimaryKey(name, cols, deferrable, deferred)
            elif kind == "u":
                idx = indexes.get(indid, {})
                uniques[rel].append(UniqueKey(name, cols, cols, nulls_not_distinct=idx.get("nnd", False),
                                              deferrable=deferrable, deferred=deferred))
            elif kind == "f":
                if frel not in rels:
                    self.warnings.append(f"foreign key {name} on {qname[rel]} points outside the user schema; ignored")
                    continue
                fks[rel].append(ForeignKey(
                    name, qname[rel], cols, qname[frel], names(frel, fkey), match=_MATCH.get(match, "simple"),
                    on_delete=_ACTION.get(dele, "no action"), on_update=_ACTION.get(upd, "no action"),
                    deferrable=deferrable, deferred=deferred, validated=validated))
                if not validated:
                    self.warnings.append(f"foreign key {name} on {qname[rel]} is NOT VALID: old rows unchecked, "
                                         f"new rows still checked")
            elif kind == "c":
                checks[rel].append(CheckConstraint(name, strip_check(definition), cols, validated, noinherit))
                if not validated:
                    self.warnings.append(f"check {name} on {qname[rel]} is NOT VALID: old rows unchecked, "
                                         f"new rows still checked")
            elif kind == "x":
                idx = indexes.get(indid, {})
                ops = excl_ops.get(con, [])
                exclusions[rel].append(ExclusionConstraint(
                    name, idx.get("method", ""), tuple(zip(idx.get("elements", ()), ops)),
                    predicate=idx.get("predicate"), deferrable=deferrable, deferred=deferred, definition=definition))

        for idx in indexes.values():                        # unique indexes that are not constraints
            if idx["unique"] and not idx["primary"] and not idx["backs_constraint"]:
                uniques[idx["rel"]].append(UniqueKey(
                    idx["name"], idx["elements"], idx["columns"], predicate=idx["predicate"],
                    nulls_not_distinct=idx["nnd"], is_constraint=False, valid=idx["valid"]))
                if not idx["valid"]:
                    self.warnings.append(f"unique index {idx['name']} on {qname[idx['rel']]} is invalid "
                                         f"(a failed CREATE INDEX CONCURRENTLY)")

        partitioning, partition_of = self._partitioning(oids, rels, attnames)
        inherits: dict[int, list[QName]] = defaultdict(list)
        inherited_by: dict[QName, list[QName]] = defaultdict(list)
        for child, parent in (self.fetch(q.INHERITANCE, oids) if oids else []):
            inherits[child].append(qname[parent])
            inherited_by[qname[parent]].append(qname[child])

        view_oids = [o for o, r in rels.items() if r["relkind"] in _VIEW_KINDS]
        reads: dict[int, list[QName]] = defaultdict(list)
        for view, ref in (self.fetch(q.VIEW_READS, view_oids) if view_oids else []):
            if ref in rels:
                reads[view].append(qname[ref])

        triggers: dict[int, list[Trigger]] = defaultdict(list)
        for rel, name, tgtype, enabled in (self.fetch(q.TRIGGERS, oids) if oids else []):
            triggers[rel].append(_trigger(name, tgtype, enabled))
        rules: dict[int, list[str]] = defaultdict(list)
        for rel, name in (self.fetch(q.RULES, oids) if oids else []):
            rules[rel].append(name)

        seed = self.inventory.row_counts if self.inventory else {}
        tables, views = {}, {}
        for oid, r in rels.items():
            qn, relkind = r["qname"], r["relkind"]
            cols = {c.name: c for c in columns[oid]}
            if relkind in _VIEW_KINDS:
                views[qn] = View(qn, _VIEW_KINDS[relkind], cols, tuple(sorted(set(reads[oid]))))
                continue
            parent, bound = partition_of.get(oid, (None, None))
            tables[qn] = Table(
                qname=qn, kind="partition" if r["is_partition"] else _TABLE_KINDS[relkind], columns=cols,
                primary_key=pk.get(oid), unique_keys=tuple(uniques[oid]), foreign_keys=tuple(fks[oid]),
                checks=tuple(checks[oid]), exclusions=tuple(exclusions[oid]), partitioning=partitioning.get(oid),
                partition_of=parent, partition_bound=bound, inherits=tuple(inherits[oid]),
                inherited_by=tuple(inherited_by[qn]), triggers=tuple(triggers[oid]), rules=tuple(rules[oid]),
                row_security=r["rls"], force_row_security=r["force"], unlogged=r["unlogged"],
                seed_rows=seed.get(str(qn), 0))

        model = SchemaModel(self.version, search_path, tables, views)
        return replace(model, warnings=tuple(self.warnings + _model_warnings(model)))

    # -------------------------------------------------------------------------
    def _indexes(self, oids, attnames) -> dict[int, dict]:
        if not oids:
            return {}
        nnd = "i.indnullsnotdistinct" if self.version >= 150000 else "false"
        out = {}
        for (idx, rel, name, unique, primary, valid, nkey, indkey, predicate, nulls_nd, method,
             backs) in self.fetch(q.INDEXES.format(nulls_not_distinct=nnd), oids):
            keys = list(indkey)[:nkey]
            texts = {}
            if any(k == 0 for k in keys):                    # expression elements: ask Postgres for their text
                texts = dict(self.fetch(q.INDEX_ELEMENTS, idx, nkey))
            elements = tuple(attnames[rel][k] if k else texts[i + 1] for i, k in enumerate(keys))
            out[idx] = dict(rel=rel, name=name, unique=unique, primary=primary, valid=valid,
                            predicate=strip_parens(predicate),
                            nnd=bool(nulls_nd), method=method, backs_constraint=backs, elements=elements,
                            columns=tuple(attnames[rel][k] for k in keys if k))
        return out

    def _partitioning(self, oids, rels, attnames):
        partitioning, partition_of = {}, {}
        parted = [o for o, r in rels.items() if r["relkind"] == "p"]
        if not parted:
            return partitioning, partition_of
        children: dict[int, list[Partition]] = defaultdict(list)
        for parent, child, text in self.fetch(q.PARTITIONS, parted):
            if child not in rels:
                continue
            try:
                bound = parse_bound(text)
            except BoundParseError as e:
                bound = PartitionBound("unknown", text)
                self.warnings.append(f"partition {rels[child]['qname']}: {e}")
            children[parent].append(Partition(rels[child]["qname"], bound))
            partition_of[child] = (rels[parent]["qname"], bound)
        for rel, strat, attrs, keydef in self.fetch(q.PARTITIONED, parted):
            key = tuple(attnames[rel][a] if a else None for a in attrs)
            partitioning[rel] = Partitioning(_STRATEGY.get(strat, strat), key, keydef, tuple(children[rel]))
        return partitioning, partition_of


class _TypeResolver:
    """Type oid + modifier -> TypeInfo, with domains unwrapped. Cached per run."""

    def __init__(self, cur, server_version: int):
        self.cur = cur
        self.version = server_version
        self._rows: dict[int, tuple] = {}
        self._cache: dict[tuple[int, int, int], TypeInfo] = {}

    def _one(self, sql, *params):
        self.cur.execute(sql, params)
        return self.cur.fetchone()

    def _all(self, sql, *params):
        self.cur.execute(sql, params)
        return self.cur.fetchall()

    def resolve(self, oid: int, typmod: int = -1, ndims: int = 0) -> TypeInfo:
        key = (oid, typmod, ndims)
        if key in self._cache:
            return self._cache[key]
        if oid not in self._rows:
            self._rows[oid] = self._one(q.TYPE, oid)
        nsp, typname, typtype, category, elem, base, typtypmod, notnull, typndims, typrelid, ext = self._rows[oid]
        qn = QName(nsp, typname)
        sql_text = self._one(q.FORMAT_TYPE, oid, typmod if typmod >= 0 else None)[0]

        if typtype == "d":
            inner = self.resolve(base, typmod if typmod >= 0 else typtypmod, ndims or typndims)
            checks = tuple(DomainCheck(qn, name, strip_check(defn), valid)
                           for name, defn, valid in self._all(q.DOMAIN_CHECKS, oid))
            info = replace(inner, name=qn, sql=sql_text, domains=(qn,) + inner.domains,
                           domain_not_null=notnull or inner.domain_not_null,
                           domain_checks=checks + inner.domain_checks)
        elif category == "A" and elem and typtype == "b":
            # A column's type modifier applies to the elements: varchar(10)[]
            info = TypeInfo(qn, sql_text, typname, Family.ARRAY, element=self.resolve(elem, typmod),
                            dimensions=ndims, extension=ext)
        elif typtype == "e":
            labels = tuple(r[0] for r in self._all(q.ENUM_LABELS, oid))
            info = TypeInfo(qn, sql_text, typname, Family.ENUM, enum_labels=labels, extension=ext)
        elif typtype == "r":
            sub = self._one(q.RANGE_SUBTYPE, oid)[0]
            info = TypeInfo(qn, sql_text, typname, Family.RANGE, subtype=self.resolve(sub), extension=ext)
        elif typtype == "m":
            sub = self._one(q.MULTIRANGE_SUBTYPE, oid)[0]
            info = TypeInfo(qn, sql_text, typname, Family.RANGE, subtype=self.resolve(sub), multirange=True,
                            extension=ext)
        elif typtype == "c":
            fields = tuple((name, self.resolve(t, m)) for name, t, m in self._all(q.COMPOSITE_FIELDS, typrelid))
            info = TypeInfo(qn, sql_text, typname, Family.COMPOSITE, fields=fields, extension=ext)
        else:
            info = TypeInfo(qn, sql_text, typname, family_of(typname), extension=ext,
                            **decode_typmod(typname, typmod))
        self._cache[key] = info
        return info


def strip_check(definition: str) -> str:
    """'CHECK ((age >= 18)) NO INHERIT NOT VALID' -> 'age >= 18'."""
    text = definition.strip()
    if text.upper().startswith("CHECK"):
        text = text[5:].strip()
    changed = True
    while changed:
        changed = False
        for suffix in (" NOT VALID", " NO INHERIT"):
            if text.upper().endswith(suffix):
                text, changed = text[: -len(suffix)].rstrip(), True
    return strip_parens(text)


def strip_parens(text: str | None) -> str | None:
    """'((a > 0))' -> 'a > 0'. Postgres wraps stored expressions in extra parentheses;
    remove only pairs that enclose the whole text, so every rule has one form."""
    if text is None:
        return None
    text = text.strip()
    while _wrapped_in_parens(text):
        text = text[1:-1].strip()
    return text


def _wrapped_in_parens(text: str) -> bool:
    """True if the outermost '(' at the start closes at the very end (ignoring strings)."""
    if not (text.startswith("(") and text.endswith(")")):
        return False
    depth = 0
    for tok in tokenize(text):
        if tok.kind != CODE:
            if depth == 0:
                return False
            continue
        for i, ch in enumerate(tok.text):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and tok.start + i != len(text) - 1:
                    return False
    return depth == 0


def _trigger(name: str, tgtype: int, enabled: str) -> Trigger:
    timing = "instead of" if tgtype & 64 else "before" if tgtype & 2 else "after"
    events = tuple(e for bit, e in ((4, "insert"), (16, "update"), (8, "delete"), (32, "truncate")) if tgtype & bit)
    return Trigger(name, timing, events, "row" if tgtype & 1 else "statement", enabled != "D")


def _model_warnings(model: SchemaModel) -> list[str]:
    out = []
    for t in model.tables.values():
        if t.partitioning is not None and not t.partitioning.partitions:
            out.append(f"{t.qname} is partitioned but has no partitions, so it cannot hold any row")
        if t.kind == "foreign table":
            out.append(f"{t.qname} is a foreign table and cannot hold test data")
        if t.inherited_by:
            out.append(f"SELECT on {t.qname} also returns rows of its inheriting tables: "
                       f"{', '.join(map(str, t.inherited_by))}")
        for c in t.columns.values():
            if c.type.family == Family.ENUM and not c.type.enum_labels:
                out.append(f"{t.qname}.{c.name}: enum {c.type.name} has no labels, so the column can only be NULL")
            if c.type.family == Family.OTHER:
                origin = f" from extension {c.type.extension}" if c.type.extension else ""
                out.append(f"{t.qname}.{c.name}: type {c.type.sql}{origin} has no value rules; "
                           f"values are written as text and may need repair")
    return out
