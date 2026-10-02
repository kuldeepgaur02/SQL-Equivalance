"""The schema model: every table, its columns and its rules, read from the catalog.

Rules are grouped the way Codd grouped integrity constraints:

  domain integrity       what a column may hold     Column.type / nullable / default / identity / generated
  entity integrity       rows are identifiable      Table.primary_key, Table.unique_keys
  referential integrity  references point at rows   Table.foreign_keys
  general integrity      any other rule on rows     Table.checks, Table.exclusions, partition bounds

Physical facts that change how data behaves (partitions, inheritance,
triggers, seed rows, row-level security) are recorded alongside.

Everything here is read-only once built; later steps never change it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Iterable, Mapping

from ..sqltext.lexer import replace_word
from ..sqltext.names import parse_qualified, quote_ident
from .names import QName
from .partitions import PartitionBound
from .types import TypeInfo

# -- domain integrity -----------------------------------------------------------


@dataclass(frozen=True)
class Column:
    name: str
    position: int                  # attnum: 1, 2, ... (gaps where columns were dropped)
    type: TypeInfo
    nullable: bool                 # False if the column or its domain is NOT NULL
    default: str | None = None     # default expression, e.g. nextval('users_id_seq'::regclass)
    identity: str | None = None    # "always" | "by default"
    generated: str | None = None   # generation expression: the column is computed, never inserted
    collation: str | None = None   # explicit collation, if not the database default
    inherited: bool = False        # comes from an INHERITS parent

    @property
    def insertable(self) -> bool:
        return self.generated is None

    @property
    def has_default(self) -> bool:
        return self.default is not None or self.identity is not None

    @property
    def is_serial(self) -> bool:
        return self.default is not None and self.default.startswith("nextval(")


# -- entity integrity -------------------------------------------------------------


@dataclass(frozen=True)
class PrimaryKey:
    name: str
    columns: tuple[str, ...]
    deferrable: bool = False
    deferred: bool = False


@dataclass(frozen=True)
class UniqueKey:
    """A UNIQUE constraint or a unique index.

    `elements` are column names or, for expression indexes, the expression
    text (lower(email)). A partial index only applies to rows matching
    `predicate`. Columns in an index's INCLUDE list are not part of the key and
    are not listed."""
    name: str
    elements: tuple[str, ...]
    columns: tuple[str, ...]              # the elements that are plain columns
    predicate: str | None = None          # partial unique index: WHERE ...
    nulls_not_distinct: bool = False      # PG 15+: NULLs collide with each other
    deferrable: bool = False
    deferred: bool = False
    is_constraint: bool = True            # False: CREATE UNIQUE INDEX
    valid: bool = True                    # False: a failed CREATE INDEX CONCURRENTLY left it invalid

    @property
    def has_expressions(self) -> bool:
        return len(self.columns) != len(self.elements)


# -- referential integrity --------------------------------------------------------


@dataclass(frozen=True)
class ForeignKey:
    name: str
    table: QName
    columns: tuple[str, ...]
    ref_table: QName
    ref_columns: tuple[str, ...]
    match: str = "simple"                 # simple: one NULL column skips the check; full: all or none NULL
    on_delete: str = "no action"          # no action | restrict | cascade | set null | set default
    on_update: str = "no action"
    deferrable: bool = False
    deferred: bool = False
    validated: bool = True                # False: NOT VALID (old rows unchecked; new rows still checked)

    @property
    def self_reference(self) -> bool:
        return self.table == self.ref_table


# -- general integrity ------------------------------------------------------------


@dataclass(frozen=True)
class CheckConstraint:
    name: str
    expression: str                       # boolean expression, without the CHECK keyword
    columns: tuple[str, ...]              # columns it mentions
    validated: bool = True                # False: NOT VALID (still enforced on new rows)
    no_inherit: bool = False
    origin: str = "table"                 # "table" | "domain"
    domain: QName | None = None


@dataclass(frozen=True)
class ExclusionConstraint:
    """EXCLUDE USING gist (room WITH =, during WITH &&): no two rows may have
    every element "match" under its operator."""
    name: str
    method: str                           # index method: gist, btree, ...
    elements: tuple[tuple[str, str], ...] # (column or expression, operator)
    predicate: str | None = None
    deferrable: bool = False
    deferred: bool = False
    definition: str = ""


@dataclass(frozen=True)
class Partition:
    table: QName
    bound: PartitionBound


@dataclass(frozen=True)
class Partitioning:
    strategy: str                         # "range" | "list" | "hash"
    key: tuple[str | None, ...]           # key columns; None where the key part is an expression
    key_sql: str                          # as Postgres prints it: RANGE (created_at)
    partitions: tuple[Partition, ...] = ()

    @property
    def has_default(self) -> bool:
        return any(p.bound.kind == "default" for p in self.partitions)


# -- physical / behavioural facts ------------------------------------------------


@dataclass(frozen=True)
class Trigger:
    name: str
    timing: str                           # before | after | instead of
    events: tuple[str, ...]               # insert | update | delete | truncate
    level: str                            # row | statement
    enabled: bool


# -- relations --------------------------------------------------------------------


@dataclass(frozen=True)
class Table:
    qname: QName
    kind: str                             # "table" | "partitioned table" | "partition" | "foreign table"
    columns: Mapping[str, Column]         # in column order
    primary_key: PrimaryKey | None = None
    unique_keys: tuple[UniqueKey, ...] = ()
    foreign_keys: tuple[ForeignKey, ...] = ()
    checks: tuple[CheckConstraint, ...] = ()
    exclusions: tuple[ExclusionConstraint, ...] = ()
    partitioning: Partitioning | None = None      # when this table is partitioned
    partition_of: QName | None = None             # when this table is a partition
    partition_bound: PartitionBound | None = None
    inherits: tuple[QName, ...] = ()              # INHERITS parents (not partitioning)
    inherited_by: tuple[QName, ...] = ()
    triggers: tuple[Trigger, ...] = ()
    rules: tuple[str, ...] = ()
    row_security: bool = False
    force_row_security: bool = False
    unlogged: bool = False
    seed_rows: int = 0                    # rows the schema SQL inserted itself

    def __post_init__(self):
        object.__setattr__(self, "columns", MappingProxyType(dict(self.columns)))

    @property
    def insert_target(self) -> bool:
        """Test data goes into plain and partitioned tables. Partitions get rows through
        their parent; foreign tables cannot hold rows."""
        return self.kind in ("table", "partitioned table")

    @property
    def insertable_columns(self) -> list[Column]:
        return [c for c in self.columns.values() if c.insertable]

    def column(self, name: str) -> Column:
        return self.columns[name]

    def fk_can_be_null(self, fk: ForeignKey) -> bool:
        """Can this FK be satisfied by NULLs instead of a parent row?
        MATCH SIMPLE: one nullable column is enough. MATCH FULL: all must be nullable."""
        nullable = [self.columns[c].nullable for c in fk.columns]
        return all(nullable) if fk.match == "full" else any(nullable)

    @property
    def all_checks(self) -> tuple[CheckConstraint, ...]:
        """Table CHECKs plus the CHECKs of column domains, with VALUE replaced by the column."""
        out = list(self.checks)
        for col in self.columns.values():
            for dc in col.type.domain_checks:
                out.append(CheckConstraint(
                    name=dc.name, expression=replace_word(dc.expression, "VALUE", quote_ident(col.name)),
                    columns=(col.name,), validated=dc.validated, origin="domain", domain=dc.domain))
        return tuple(out)


@dataclass(frozen=True)
class View:
    qname: QName
    kind: str                             # "view" | "materialized view"
    columns: Mapping[str, Column]
    reads: tuple[QName, ...]              # tables and views it selects from

    def __post_init__(self):
        object.__setattr__(self, "columns", MappingProxyType(dict(self.columns)))


@dataclass(frozen=True)
class SchemaModel:
    server_version: int
    search_path: tuple[str, ...]          # effective, as Postgres resolves unqualified names
    tables: Mapping[QName, Table]
    views: Mapping[QName, View] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "tables", MappingProxyType(dict(self.tables)))
        object.__setattr__(self, "views", MappingProxyType(dict(self.views)))

    # -- lookup -----------------------------------------------------------------
    def resolve(self, name: str | QName | Iterable[str]) -> QName | None:
        """Find a table or view the way Postgres would: 'users' through the search
        path, 'app.users' directly, '"Users"' case-sensitively."""
        if isinstance(name, QName):
            return name if name in self.tables or name in self.views else None
        parts = parse_qualified(name) if isinstance(name, str) else tuple(name)
        if len(parts) == 2:
            q = QName(*parts)
            return q if q in self.tables or q in self.views else None
        if len(parts) == 1:
            for schema in self.search_path:
                q = QName(schema, parts[0])
                if q in self.tables or q in self.views:
                    return q
        return None

    def table(self, name: str | QName) -> Table:
        q = self.resolve(name)
        if q is None or q not in self.tables:
            raise KeyError(f"no table {name!r}")
        return self.tables[q]

    @property
    def insert_targets(self) -> list[Table]:
        return [t for t in self.tables.values() if t.insert_target]

    def references_to(self, qname: QName) -> list[ForeignKey]:
        """FKs in other tables (or this one) that point at `qname`."""
        return [fk for t in self.tables.values() for fk in t.foreign_keys if fk.ref_table == qname]

    def stats(self) -> dict[str, int]:
        ts = list(self.tables.values())
        return {
            "tables": sum(t.kind == "table" for t in ts),
            "partitioned_tables": sum(t.kind == "partitioned table" for t in ts),
            "partitions": sum(t.kind == "partition" for t in ts),
            "foreign_tables": sum(t.kind == "foreign table" for t in ts),
            "views": len(self.views),
            "columns": sum(len(t.columns) for t in ts),
            "primary_keys": sum(t.primary_key is not None for t in ts),
            "unique_keys": sum(len(t.unique_keys) for t in ts),
            "foreign_keys": sum(len(t.foreign_keys) for t in ts),
            "checks": sum(len(t.all_checks) for t in ts),
            "exclusions": sum(len(t.exclusions) for t in ts),
        }
