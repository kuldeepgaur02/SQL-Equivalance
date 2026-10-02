"""Parse a query with sqlglot and resolve every name against the schema model.

  - table names are resolved the way Postgres does (search_path, case folding,
    CTE names shadowing tables);
  - sqlglot's qualify() attaches every column to its source alias and expands *;
  - a column read through a CTE or subquery is traced back to its base table
    when it is passed through unchanged (SELECT o.amount AS amt ...).
"""
from __future__ import annotations

import logging

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, traverse_scope
from sqlglot.schema import MappingSchema

from ..schema.model import SchemaModel
from ..schema.names import QName
from ..schema.types import Family, TypeInfo
from .model import ColumnRef

log = logging.getLogger(__name__)

_SQLGLOT_TYPE = {
    Family.INTEGER: "BIGINT", Family.NUMERIC: "DECIMAL", Family.FLOAT: "DOUBLE", Family.MONEY: "DECIMAL",
    Family.TEXT: "TEXT", Family.BOOLEAN: "BOOLEAN", Family.DATE: "DATE", Family.TIME: "TIME",
    Family.TIMESTAMP: "TIMESTAMP", Family.INTERVAL: "INTERVAL", Family.UUID: "UUID", Family.JSON: "JSONB",
    Family.BYTES: "VARBINARY",
}


class ParsedQuery:
    def __init__(self, sql: str, model: SchemaModel):
        self.model = model
        self.warnings: list[str] = []
        self.tree: exp.Expression = sqlglot.parse_one(sql, read="postgres")   # ParseError propagates
        self.cte_names = {c.alias_or_name.lower() for c in self.tree.find_all(exp.CTE)}
        self.unresolved_tables: list[str] = []
        self._attach_schemas()
        try:
            self.tree = qualify(self.tree, schema=MappingSchema(_mapping(model), dialect="postgres", normalize=False),
                                dialect="postgres", validate_qualify_columns=False, quote_identifiers=False,
                                identify=False)
        except Exception as e:                       # qualify is best effort; resolution falls back below
            self.warnings.append(f"could not qualify every column ({type(e).__name__}: {e}); "
                                 f"some filters may not be attributed to a table")
        self.scopes: list[Scope] = list(traverse_scope(self.tree))

    # -- tables --------------------------------------------------------------------
    def _attach_schemas(self) -> None:
        """Give every table reference its schema, as Postgres would resolve it."""
        for t in self.tree.find_all(exp.Table):
            if not isinstance(t.this, exp.Identifier):
                continue                              # FROM generate_series(...) and the like
            name = _ident(t.this)
            if not t.db and name.lower() in self.cte_names:
                continue
            q = self.model.resolve((_ident(t.args["db"]), name) if t.db else (name,))
            if q is None:
                self.unresolved_tables.append(t.sql("postgres"))
                continue
            t.set("db", exp.Identifier(this=q.schema, quoted=True))
            t.set("this", exp.Identifier(this=q.name, quoted=True))

    def table_of(self, node: exp.Table) -> QName | None:
        if not node.db or not isinstance(node.this, exp.Identifier):
            return None
        q = QName(node.db, node.name)
        return q if q in self.model.tables or q in self.model.views else None

    # -- columns -------------------------------------------------------------------
    def resolve_column(self, col: exp.Column, scope: Scope, depth: int = 0) -> ColumnRef | None:
        if depth > 10:
            return None
        name = col.name
        sources = scope.sources
        source = sources.get(col.table) if col.table else None
        if source is None and not col.table:          # unqualified: the one source that has the column
            owners = [s for s in sources.values() if self._has_column(s, name)]
            source = owners[0] if len(owners) == 1 else None
        if isinstance(source, exp.Table):
            q = self.table_of(source)
            if q is None:
                return None
            rel = self.model.tables.get(q) or self.model.views.get(q)
            return ColumnRef(q, name) if name in rel.columns else None
        if isinstance(source, Scope) and isinstance(source.expression, exp.Select):
            for proj in source.expression.expressions:
                if proj.alias_or_name == name:
                    inner = proj.unalias()
                    if isinstance(inner, exp.Column):
                        return self.resolve_column(inner, source, depth + 1)
                    return None
        return None

    def _has_column(self, source, name: str) -> bool:
        if isinstance(source, exp.Table):
            q = self.table_of(source)
            rel = (self.model.tables.get(q) or self.model.views.get(q)) if q else None
            return rel is not None and name in rel.columns
        if isinstance(source, Scope) and isinstance(source.expression, exp.Select):
            return any(p.alias_or_name == name for p in source.expression.expressions)
        return False


def scope_label(scope: Scope) -> str:
    kind = scope.scope_type.name.lower() if getattr(scope, "scope_type", None) else ""
    if scope.is_root:
        return "main"
    if scope.is_cte:
        parent = scope.expression.parent
        return f"cte:{parent.alias_or_name}" if isinstance(parent, exp.CTE) else "cte"
    if scope.is_derived_table:
        return "derived"
    if kind == "union" or getattr(scope, "is_union", False):
        return "union"
    return "subquery"


def _mapping(model: SchemaModel) -> dict:
    mapping: dict = {}
    for rel in list(model.tables.values()) + list(model.views.values()):
        cols = {c.name: _type_name(c.type) for c in rel.columns.values()}
        mapping.setdefault(rel.qname.schema, {})[rel.qname.name] = cols or {"__no_columns__": "TEXT"}
    return mapping


def _type_name(t: TypeInfo) -> str:
    if t.family == Family.ARRAY and t.element is not None:
        return f"ARRAY<{_type_name(t.element)}>"
    return _SQLGLOT_TYPE.get(t.family, "TEXT")


def _ident(node) -> str:
    """Identifier text as Postgres sees it: unquoted folds to lower case."""
    if isinstance(node, exp.Identifier):
        return node.this if node.quoted else node.this.lower()
    return str(node)
