"""What Step 6 produces: the rows that are really in the database, and how they got there."""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from ..schema.names import QName

# How a failed insert was repaired:
CODE = "code"                  # PK / UNIQUE, FK, NOT NULL (the diagram: "fix by code")
LLM = "llm"                    # CHECK / unknown (the diagram: "LLM")
RULES = "rules"                # CHECK / unknown in --no-llm mode
RULES_AFTER_LLM = "rules (llm failed)"


@dataclass(frozen=True)
class Repair:
    table: QName
    attempt: int                       # 1, 2, 3
    category: str                      # classify.UNIQUE / FOREIGN_KEY / ... / UNKNOWN
    sqlstate: str
    constraint: str | None
    message: str
    method: str | None                 # CODE / LLM / RULES / RULES_AFTER_LLM; None: no fix found
    changed: Mapping[str, tuple[Any, Any]] = field(default_factory=dict)   # column -> (old, new)
    note: str = ""


@dataclass(frozen=True)
class LoadResult:
    rows: Mapping[QName, Mapping[str, Any]]          # the base row of each table, as Postgres stored it
    skipped: Mapping[QName, str]                      # plan tables that got no row, and why
    repairs: tuple[Repair, ...] = ()
    updates: tuple[str, ...] = ()                     # planned UPDATEs applied
    failed_updates: tuple[str, ...] = ()              # planned UPDATEs that failed (FK left NULL)
    snapshot: Mapping[QName, tuple[Mapping[str, Any], ...]] = field(default_factory=dict)  # every row in the DB
    warnings: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "rows", MappingProxyType(dict(self.rows)))
        object.__setattr__(self, "skipped", MappingProxyType(dict(self.skipped)))
        object.__setattr__(self, "snapshot", MappingProxyType(dict(self.snapshot)))

    @property
    def total_rows(self) -> int:
        return sum(len(r) for r in self.snapshot.values())
