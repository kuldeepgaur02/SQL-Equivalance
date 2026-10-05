"""Step 9 as the last pipeline step: stop on a counterexample, or hand off to the mutation stage.

Either way, one JSON document per case is written next to its attempt log
(runs/<case>/handoff-<time>.json), after the replay script has been checked
in a fresh database (settings.verify_handoff).
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from .build import build_handoff, status_of
from .replay import data_sql, verify

if TYPE_CHECKING:
    from ..runner.batch import CaseContext

log = logging.getLogger(__name__)


def environment(ctx: "CaseContext") -> dict:
    """How this result was produced: needed to reproduce it, or to compare two runs fairly."""
    s = ctx.settings
    with ctx.conn.cursor() as cur:
        cur.execute("SELECT current_setting('server_version'), datcollate FROM pg_database "
                    "WHERE datname = current_database()")
        version, collation = cur.fetchone()
    ctx.conn.rollback()
    return {"postgres": version, "collation": collation, "time_zone": s.session.get("TimeZone"),
            "schema_memory": s.use_schema_memory, "llm": s.llm_enabled,
            "llm_model": s.llm_model if s.llm_enabled else None, "max_rebuilds": s.max_rebuilds}


def hand_off(ctx: "CaseContext") -> None:
    state = ctx.state
    comparison = state["comparison"]
    with ctx.journal.timed("handoff", "stop on a counterexample, or hand off to the mutation stage") as d:
        env = environment(ctx)
        script = data_sql(ctx.conn, state["schema"], state["load"].snapshot, header=[
            f"cexgen replay for case {ctx.case.name}: recreates the data of its hand-off.",
            "Run after the schema SQL, as the owner of the tables (no superuser needed).",
            f"Produced on PostgreSQL {env['postgres']} with collation {env['collation']} and time zone "
            f"{env['time_zone']}; text comparisons and ordering can differ under another collation."])
        verified = None
        if ctx.settings.verify_handoff:
            verified = verify(ctx.settings, ctx.case.schema_sql, script, ctx.case.q1, ctx.case.q2, comparison)
        doc = build_handoff(ctx, script, verified)
        doc["environment"] = env
        path = None
        if ctx.journal.path is not None:
            path = ctx.journal.path.parent / f"handoff-{ctx.journal.path.stem}.json"
            path.write_text(json.dumps(doc, indent=1, ensure_ascii=False))
        log.info("%s: %s; replay %s; hand-off file %s", ctx.case.name, doc["status"],
                 {True: "verified", False: "NOT reproduced", None: "not verified"}[doc["data"]["replay_verified"]], path)
        state["handoff"] = doc
        state["handoff_path"] = path
        d.update(status=status_of(comparison), file=str(path) if path else None,
                 replay_verified=doc["data"]["replay_verified"], replay_note=doc["data"]["replay_note"],
                 rounds=len(doc["attempts"]))
