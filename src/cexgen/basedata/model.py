"""The base data: one row per table, and where every value came from."""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from ..schema.model import ForeignKey
from ..schema.names import QName

# Where a value came from (kept for repair, the mutation stage and the final explanation):
DEFAULT = "default"        # hardcoded default for the type (ignores the queries)
ENUM = "enum"              # first label of the enum
RULE = "rule"              # changed to satisfy a schema rule (CHECK, domain, partition bound)
FK = "fk"                  # copied from the referenced row (or the row itself)
FK_NULL = "fk-null"        # left NULL by the insert plan (set later by UPDATE, or for good)
LLM = "llm"                # JSON / ARRAY value from the LLM
LLM_MEMORY = "llm-memory"  # an LLM answer reused from schema memory
JSON_RULE = "json-rule"    # JSON / ARRAY value from the rule table
MEMORY = "memory"          # a row known to insert cleanly, from schema memory


@dataclass(frozen=True)
class Cell:
    value: Any
    source: str
    note: str = ""


@dataclass(frozen=True)
class PendingUpdate:
    """An FK inserted as NULL (null_then_update): the values to set once all rows exist."""
    table: QName
    fk: ForeignKey
    values: Mapping[str, Any]


@dataclass(frozen=True)
class BaseData:
    rows: Mapping[QName, Mapping[str, Cell]]      # in insert-plan order
    updates: tuple[PendingUpdate, ...] = ()
    unsatisfied: tuple[str, ...] = ()             # rules generation could not meet: for Step 6's repair
    warnings: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "rows", MappingProxyType({t: MappingProxyType(dict(r)) for t, r in self.rows.items()}))

    def values(self, table: QName) -> dict[str, Any]:
        return {c: cell.value for c, cell in self.rows[table].items()}
