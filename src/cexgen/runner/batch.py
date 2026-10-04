"""The outer loop: many cases, one database per schema.

    cases grouped by schema text (input order kept)
      for each schema:   SchemaWorkspace (database + tables + seed state)  +  SchemaMemory
        for each case:   reset()  ->  Journal  ->  run the pipeline steps  ->  close journal
        save memory, drop database

Pipeline steps are plain functions `step(ctx)`, run in order. Steps 2-9 are
added to PIPELINE as they are built. A step that raises a CexError fails its
case only; the batch moves on to the next case. If a schema cannot be built,
every case using it is marked failed and the batch moves on.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .. import __version__
from ..basedata.step import build_base_data
from ..compare.step import compare_queries
from ..config import Settings, get_settings
from ..db.connection import safe_dsn
from ..db.workspace import SchemaWorkspace, schema_fingerprint
from ..errors import CexError
from ..input import Case
from ..handoff.step import hand_off
from ..insertion.step import insert_base
from ..journal import Journal
from ..memory import SchemaMemory
from ..observe.format import CURRENT_CASE
from ..ordering.step import plan_order
from ..queries.step import parse_queries
from ..rebuild.loop import rebuild_until_rows
from ..schema.step import parse_schema

log = logging.getLogger(__name__)


@dataclass
class CaseContext:
    """Everything a pipeline step can use. Steps keep their results in `state`."""
    case: Case
    workspace: SchemaWorkspace
    journal: Journal
    memory: SchemaMemory
    settings: Settings
    state: dict[str, Any] = field(default_factory=dict)

    @property
    def conn(self):
        return self.workspace.conn


Step = Callable[[CaseContext], None]

# How each step is announced in the live view and the run log (the diagram's boxes).
STEP_TITLES: dict = {}


def _titles() -> dict:
    if not STEP_TITLES:
        STEP_TITLES.update({
            parse_schema: "Step 2 · Parse schema (Postgres catalog)",
            parse_queries: "Step 3 · Parse queries (Postgres check + sqlglot)",
            plan_order: "Step 4 · Table order (FK graph)",
            build_base_data: "Step 5 · Base data, one row per table",
            insert_base: "Step 6 · INSERT into Postgres + repair loop",
            compare_queries: "Step 7 · Run Q1 and Q2, compare",
            rebuild_until_rows: "Step 8 · Rebuild the base if both results are empty",
            hand_off: "Step 9 · Hand-off to the mutation stage (or stop: counterexample)",
        })
    return STEP_TITLES


# Steps in order; each later step is appended as it is built.
PIPELINE: list[Step] = [parse_schema, parse_queries, plan_order, build_base_data, insert_base, compare_queries,
                        rebuild_until_rows, hand_off]


@dataclass
class CaseRun:
    case: Case
    status: str                          # "ok" | "failed"
    journal_path: Path | None
    error: str | None = None
    state: dict[str, Any] = field(default_factory=dict)


@dataclass
class SchemaRun:
    fingerprint: str
    cases: list[CaseRun]
    database: str | None = None
    inventory: Any = None               # db.inventory.Inventory, when the schema was built
    memory_path: Path | None = None
    error: str | None = None


def run_cases(cases: list[Case], settings: Settings | None = None,
              pipeline: list[Step] | None = None) -> list[SchemaRun]:
    settings = settings or get_settings()
    steps = PIPELINE if pipeline is None else pipeline
    results = []
    for schema_sql, group in _group_by_schema(cases).items():
        results.append(_run_schema(schema_sql, group, settings, steps))
    return results


def _run_schema(schema_sql: str, cases: list[Case], settings: Settings, steps: list[Step]) -> SchemaRun:
    run = SchemaRun(fingerprint=schema_fingerprint(schema_sql), cases=[])
    log.info("schema %s: building its sandbox database for %d case(s)", run.fingerprint, len(cases))
    try:
        workspace = SchemaWorkspace(schema_sql, settings)
    except CexError as e:
        run.error = str(e)
        log.error("schema %s could not be built: %s", run.fingerprint, str(e).splitlines()[0])
        for case in cases:
            with Journal.for_case(case.name, settings.runs_dir) as j:
                _start(j, case, settings, run.fingerprint)
                j.record("setup", "build schema", "failed", error=e)
                run.cases.append(CaseRun(case, "failed", j.path, error=str(e)))
        return run

    with workspace:
        run.database, run.inventory = workspace.database, workspace.inventory
        memory = SchemaMemory.open(workspace.fingerprint, workspace.server_version, settings)
        run.memory_path = memory.path
        try:
            for case in cases:
                run.cases.append(_run_case(case, workspace, memory, settings, steps))
        finally:
            memory.save()                # keep what was learned even if the batch is interrupted
    return run


def _run_case(case: Case, ws: SchemaWorkspace, memory: SchemaMemory, settings: Settings,
              steps: list[Step]) -> CaseRun:
    token = CURRENT_CASE.set(case.name)
    try:
        return _run_case_logged(case, ws, memory, settings, steps)
    finally:
        CURRENT_CASE.reset(token)


def _run_case_logged(case: Case, ws: SchemaWorkspace, memory: SchemaMemory, settings: Settings,
                     steps: list[Step]) -> CaseRun:
    log.info("case %s: start (%s)", case.name, case.source)
    with Journal.for_case(case.name, settings.runs_dir) as j:
        _start(j, case, settings, ws.fingerprint)
        ctx = CaseContext(case, ws, j, memory, settings)
        try:
            j.begin("run", "Step 1 · Input checked; reset the sandbox to the schema's initial state")
            with j.timed("setup", "reset database to the schema's initial state") as d:
                ws.reset()
                d.update(database=ws.database, seed_rows=ws.seed.row_count,
                         tables=[t.qualified for t in ws.inventory.tables],
                         warnings=ws.inventory.warnings(), schema_memory=memory.key if memory.persistent else None)
            if case.identical_queries:
                j.record("setup", "Q1 and Q2 are the same query text", "info")
            titles = _titles()
            for step in steps:
                j.begin("run", titles.get(step, f"step {getattr(step, '__name__', step)}"))
                step(ctx)
        except CexError as e:
            j.record("run", "case stopped", "failed", error=e)
            return CaseRun(case, "failed", j.path, error=str(e), state=ctx.state)
        except Exception as e:           # a bug: log it in the journal, keep the batch going
            log.exception("case %s crashed", case.name)
            j.record("run", "case crashed", "failed", error=e)
            return CaseRun(case, "failed", j.path, error=f"{type(e).__name__}: {e}", state=ctx.state)
        j.record("run", "case finished", "ok")
        return CaseRun(case, "ok", j.path, state=ctx.state)


def _start(j: Journal, case: Case, settings: Settings, fingerprint: str) -> None:
    j.record("run", "start", "info", detail={
        "case": case.name, "source": case.source, "meta": dict(case.meta),
        "q1": case.q1, "q2": case.q2, "schema_fingerprint": fingerprint,
        "cexgen_version": __version__, "dsn": safe_dsn(settings.dsn),
        "schema_memory": settings.use_schema_memory,
    })


def _group_by_schema(cases: list[Case]) -> dict[str, list[Case]]:
    groups: dict[str, list[Case]] = {}
    for case in cases:
        groups.setdefault(case.schema_sql, []).append(case)
    return groups
