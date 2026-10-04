"""One round of "data -> INSERT -> compare", kept with its exact result.

Round 0 is the base (Steps 5-7); rounds 1.. are Step 8's rebuilds. The list of
rounds is what the mutation stage receives as "every attempt already made with
its exact result".
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..basedata.model import BaseData
from ..compare.model import Comparison
from ..insertion.model import LoadResult


@dataclass(frozen=True)
class Round:
    number: int                    # 0 = the base, 1.. = rebuilds
    method: str                    # "base" | "llm" | "rules" | "rules (llm failed)"
    base: BaseData                 # the rows that were planned
    load: LoadResult               # what Postgres stored (after repairs)
    comparison: Comparison         # Q1 vs Q2 on it
    changes: tuple[str, ...] = ()  # e.g. "public.orders.amount = Decimal('100.01')"
    notes: tuple[str, ...] = field(default_factory=tuple)
