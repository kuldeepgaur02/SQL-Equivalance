"""Step 2 as a pipeline step: give each case the schema model.

The model is read once per schema and cached on the workspace, so 50 cases on
one schema read the catalog once.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .reader import read_schema

if TYPE_CHECKING:
    from ..runner.batch import CaseContext

CACHE_KEY = "schema_model"


def parse_schema(ctx: "CaseContext") -> None:
    ws = ctx.workspace
    with ctx.journal.timed("parse_schema", "read the schema model from the catalog") as detail:
        model = ws.cache.get(CACHE_KEY)
        detail["cached"] = model is not None
        if model is None:
            model = read_schema(ws.conn, ws.inventory)
            ws.cache[CACHE_KEY] = model
        ctx.state["schema"] = model
        logging.getLogger(__name__).debug("schema model: %s%s", model.stats(), " (cached)" if detail["cached"] else "")
        detail["stats"] = model.stats()
        detail["warnings"] = list(model.warnings)
