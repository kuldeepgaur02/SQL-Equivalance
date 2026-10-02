"""The insert plan: which tables get rows, in which order, and how every
foreign key will be satisfied, decided before any data exists."""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from ..schema.model import ForeignKey
from ..schema.names import QName

# How a foreign key is satisfied:
PARENT = "parent"                  # the parent row is inserted in an earlier step
NULL_THEN_UPDATE = "null_then_update"   # insert with the FK NULL, set it by UPDATE once all rows exist
SELF = "self"                      # NOT NULL self-reference: the row points at itself (id = 1, parent_id = 1)
DEFERRED = "deferred"              # cycle of NOT NULL FKs: checked at the end of the step's transaction
NULL = "null"                      # the parent table cannot get rows; the FK stays NULL


@dataclass(frozen=True)
class FkPlan:
    fk: ForeignKey
    strategy: str
    reason: str
    null_columns: tuple[str, ...] = ()   # for null / null_then_update: the columns to leave NULL

    @property
    def key(self) -> tuple[QName, str]:
        return self.fk.table, self.fk.name


@dataclass(frozen=True)
class Step:
    tables: tuple[QName, ...]            # one table, or every table of a NOT NULL cycle
    deferred: tuple[ForeignKey, ...] = ()   # SET CONSTRAINTS ... DEFERRED for this step's transaction

    @property
    def kind(self) -> str:
        return "cycle" if len(self.tables) > 1 else "single"


@dataclass(frozen=True)
class InsertPlan:
    steps: tuple[Step, ...]
    fks: tuple[FkPlan, ...]                     # every FK of every table that gets rows
    updates: tuple[FkPlan, ...]                 # null_then_update FKs, applied after all steps
    skipped: Mapping[QName, str]                # table -> why it gets no rows
    make_deferrable: tuple[ForeignKey, ...]     # NOT DEFERRABLE FKs the sandbox must make deferrable
    scope: str                                  # "queries" | "all"
    warnings: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "skipped", MappingProxyType(dict(self.skipped)))

    @property
    def order(self) -> list[QName]:
        return [t for s in self.steps for t in s.tables]

    def fk_plan(self, table: QName, fk_name: str) -> FkPlan | None:
        return next((p for p in self.fks if p.key == (table, fk_name)), None)

    def fks_of(self, table: QName) -> list[FkPlan]:
        return [p for p in self.fks if p.fk.table == table]
