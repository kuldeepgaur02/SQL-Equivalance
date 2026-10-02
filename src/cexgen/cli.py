"""Command line. Each step adds a subcommand; Step 1 provides:

  cexgen check PATH        validate the case(s) and run the pipeline steps built so far
  cexgen schema PATH       show the schema model (Step 2)
  cexgen queries PATH      show what Step 3 found in Q1 / Q2
  cexgen plan PATH         show the insert plan (Step 4)
  cexgen base PATH         show the base data (Step 5)
  cexgen load PATH         insert it, with the repair loop (Step 6)
  cexgen cleanup           drop sandbox databases left behind by killed runs
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import timedelta

from .config import get_settings
from .db import SchemaWorkspace, cleanup_orphans
from .errors import CexError
from .input import load_cases
from .runner import run_cases
from .basedata import build_base_data, describe_base
from .insertion import describe_load, insert_base
from .ordering import describe_plan, plan_order
from .queries import describe_pair, describe_rules, parse_queries
from .schema import describe, parse_schema, read_schema, to_dict


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="cexgen")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("check", help="validate cases and create their tables in a sandbox database")
    p.add_argument("path", help="a case folder, a batch .json, or a folder of cases")
    p.add_argument("--keep-db", action="store_true", help="keep the sandbox database for inspection")
    p.add_argument("--fresh", action="store_true", help="ignore saved schema memory (clean evaluation run)")
    p.add_argument("--runs-dir", help="where attempt logs and schema memory go (default: runs/ or $CEX_RUNS_DIR)")
    p.add_argument("--no-llm", action="store_true", help="baseline mode: the rule table instead of the LLM")
    p.set_defaults(func=_check)

    p = sub.add_parser("schema", help="show the schema model (Step 2) for the case(s) under PATH")
    p.add_argument("path", help="a case folder, a batch .json, or a folder of cases")
    p.add_argument("--json", action="store_true", help="print the full model as JSON")
    p.set_defaults(func=_schema)

    p = sub.add_parser("queries", help="show what Step 3 found in Q1 / Q2 (and the schema's rules)")
    p.add_argument("path", help="a case folder, a batch .json, or a folder of cases")
    p.add_argument("--rules", action="store_true", help="also show how the schema's CHECK / index rules were read")
    p.add_argument("--runs-dir", help="where attempt logs go (default: runs/ or $CEX_RUNS_DIR)")
    p.set_defaults(func=_queries)

    p = sub.add_parser("plan", help="show the insert plan (Step 4): table order and how each FK is satisfied")
    p.add_argument("path", help="a case folder, a batch .json, or a folder of cases")
    p.add_argument("--runs-dir", help="where attempt logs go (default: runs/ or $CEX_RUNS_DIR)")
    p.set_defaults(func=_plan)

    p = sub.add_parser("base", help="show the base data (Step 5): one row per table, and where each value came from")
    p.add_argument("path", help="a case folder, a batch .json, or a folder of cases")
    p.add_argument("--no-llm", action="store_true", help="baseline mode: the rule table instead of the LLM")
    p.add_argument("--fresh", action="store_true", help="ignore saved schema memory")
    p.add_argument("--runs-dir", help="where attempt logs and schema memory go (default: runs/ or $CEX_RUNS_DIR)")
    p.set_defaults(func=_base)

    p = sub.add_parser("load", help="run Steps 1-6 and show what was inserted and repaired")
    p.add_argument("path", help="a case folder, a batch .json, or a folder of cases")
    p.add_argument("--no-llm", action="store_true", help="baseline mode: the rule table instead of the LLM")
    p.add_argument("--fresh", action="store_true", help="ignore saved schema memory")
    p.add_argument("--runs-dir", help="where attempt logs and schema memory go (default: runs/ or $CEX_RUNS_DIR)")
    p.set_defaults(func=_load)

    p = sub.add_parser("cleanup", help="drop leftover sandbox databases")
    p.add_argument("--older-than", type=int, default=60, metavar="MINUTES")
    p.set_defaults(func=_cleanup)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    try:
        return args.func(args)
    except CexError as e:
        print(f"error: {e}", file=sys.stderr)
        return e.exit_code
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


def _check(args) -> int:
    settings = get_settings()
    changes = {"keep_db": True} if args.keep_db else {}
    if args.fresh:
        changes["use_schema_memory"] = False
    if args.runs_dir:
        changes["runs_dir"] = args.runs_dir
    if args.no_llm:
        changes["llm_enabled"] = False
    settings = settings.with_(**changes)

    cases = load_cases(args.path, settings)
    print(f"{len(cases)} case(s) from {args.path}")
    failed = 0
    for run in run_cases(cases, settings):
        print(f"\nschema {run.fingerprint}" + (f"  (database {run.database}, kept)" if settings.keep_db and run.database else ""))
        if run.error:
            print("  " + run.error.replace("\n", "\n  "))
        if run.inventory is not None:
            print("  " + run.inventory.summary().replace("\n", "\n  "))
            for w in run.inventory.warnings():
                print(f"  warning: {w}")
        print(f"  schema memory: {run.memory_path or 'off (--fresh)'}")
        for c in run.cases:
            flag = "  (Q1 and Q2 are the same query text)" if c.case.identical_queries else ""
            print(f"  [{c.status}] {c.case.name}{flag}\n         log: {c.journal_path}")
            if c.error and not run.error:
                print(f"         error: {c.error}")
            failed += c.status != "ok"
    return 1 if failed else 0


def _schema(args) -> int:
    settings = get_settings()
    cases = load_cases(args.path, settings)
    seen: dict[str, list[str]] = {}
    for c in cases:
        seen.setdefault(c.schema_sql, []).append(c.name)
    out = []
    for schema_sql, names in seen.items():
        with SchemaWorkspace(schema_sql, settings) as ws:
            model = read_schema(ws.conn, ws.inventory)
        if args.json:
            out.append({"cases": names, "fingerprint": ws.fingerprint, "model": to_dict(model)})
        else:
            print(f"== schema {ws.fingerprint}, used by: {', '.join(names)}\n")
            print(describe(model) + "\n")
    if args.json:
        print(json.dumps(out, indent=1, default=str))
    return 0


def _queries(args) -> int:
    settings = get_settings().with_(use_schema_memory=False)
    if args.runs_dir:
        settings = settings.with_(runs_dir=args.runs_dir)
    cases = load_cases(args.path, settings)
    shown_rules = set()
    for run in run_cases(cases, settings, pipeline=[parse_schema, parse_queries]):
        if run.error:
            print(f"schema {run.fingerprint}: {run.error}")
        for c in run.cases:
            print(f"== {c.case.name}")
            if c.status != "ok":
                print(f"error: {c.error}\n")
                continue
            print(describe_pair(c.state["queries"]) + "\n")
            if args.rules and run.fingerprint not in shown_rules:
                shown_rules.add(run.fingerprint)
                print("schema rules:\n" + (describe_rules(c.state["rules"]) or "(none)") + "\n")
    return 0


def _plan(args) -> int:
    settings = get_settings().with_(use_schema_memory=False)
    if args.runs_dir:
        settings = settings.with_(runs_dir=args.runs_dir)
    for run in run_cases(load_cases(args.path, settings), settings, pipeline=[parse_schema, parse_queries, plan_order]):
        if run.error:
            print(f"schema {run.fingerprint}: {run.error}")
        for c in run.cases:
            print(f"== {c.case.name}")
            print((describe_plan(c.state["plan"]) if c.status == "ok" else f"error: {c.error}") + "\n")
    return 0


def _base(args) -> int:
    changes = {"llm_enabled": not args.no_llm}
    if args.fresh:
        changes["use_schema_memory"] = False
    if args.runs_dir:
        changes["runs_dir"] = args.runs_dir
    settings = get_settings().with_(**changes)
    failed = 0
    for run in run_cases(load_cases(args.path, settings), settings,
                         pipeline=[parse_schema, parse_queries, plan_order, build_base_data]):
        if run.error:
            print(f"schema {run.fingerprint}: {run.error}")
        for c in run.cases:
            print(f"== {c.case.name}")
            print((describe_base(c.state["base"]) if c.status == "ok" else f"error: {c.error}") + "\n")
            failed += c.status != "ok"
    return 1 if failed else 0


def _load(args) -> int:
    changes = {"llm_enabled": not args.no_llm}
    if args.fresh:
        changes["use_schema_memory"] = False
    if args.runs_dir:
        changes["runs_dir"] = args.runs_dir
    settings = get_settings().with_(**changes)
    failed = 0
    for run in run_cases(load_cases(args.path, settings), settings,
                         pipeline=[parse_schema, parse_queries, plan_order, build_base_data, insert_base]):
        if run.error:
            print(f"schema {run.fingerprint}: {run.error}")
        for c in run.cases:
            print(f"== {c.case.name}")
            print((describe_load(c.state["load"]) if c.status == "ok" else f"error: {c.error}") + "\n")
            failed += c.status != "ok"
    return 1 if failed else 0


def _cleanup(args) -> int:
    dropped = cleanup_orphans(get_settings(), older_than=timedelta(minutes=args.older_than))
    print(f"dropped {len(dropped)} sandbox database(s)" + (": " + ", ".join(dropped) if dropped else ""))
    return 0
