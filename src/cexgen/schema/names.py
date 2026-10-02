"""Schema-qualified names. Every table, view and type is identified by one,
so public.users and app.users can never be confused."""
from __future__ import annotations

from dataclasses import dataclass

from ..sqltext.names import quote_ident


@dataclass(frozen=True, order=True)
class QName:
    schema: str
    name: str

    def __str__(self) -> str:
        return f"{quote_ident(self.schema)}.{quote_ident(self.name)}"

    @property
    def sql(self) -> str:
        """Always-valid SQL reference (same as str())."""
        return str(self)
