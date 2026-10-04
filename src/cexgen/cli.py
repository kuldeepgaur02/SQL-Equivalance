"""Command line.

  cexgen check PATH        run the whole pipeline (Steps 1-9): results, hand-offs
  cexgen schema PATH       show the schema model (Step 2)
  cexgen queries PATH      show what Step 3 found in Q1 / Q2
  cexgen plan PATH         show the insert plan (Step 4)
  cexgen base PATH         show the base data (Step 5)
  cexgen load PATH         insert it, with the repair loop (Step 6)
  cexgen logs              show the newest run log (--list, --clean DAYS)
  cexgen cleanup           drop sandbox databases left behind by killed runs

Every command takes -v (show the flow live), --debug (show everything live) and
--no-run-log. Unless --no-run-log, each run writes runs/_logs/<time>-<command>.log.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

from .config import get_settings
from .db import SchemaWorkspace, cleanup_orphans
from .errors import CexError
from .input import load_cases
from .observe import Session
from .runner import run_cases
from .basedata import build_base_data, describe_base
from .compare import describe_comparison
from .insertion import describe_load, insert_base
from .ordering import describe_plan, plan_order
from .queries import describe_pair, describe_rules, parse_queries
from .schema import describe, parse_schema, read_schema, to_dict


def _selection(p) -> None:
    p.add_argument("--sample", type=int, metavar="N", help="run a fixed random subset of N cases")
    p.add_argument("--seed", type=int, default=0, help="seed for --sample (same seed, same cases)")


def _observe_flags(p, defaults: bool) -> None:
    kw = {} if defaults else {"default": argparse.SUPPRESS}
    p.add_argument("-v", "--verbose", action="store_const", const=1, dest="verbosity",
                   help="show the flow live: each step, each action and its result", **({"default": 0} if defaults else kw))
    p.add_argument("--debug", action="store_const", const=2, dest="verbosity",
                   help="show everything live: also SQL, sandbox, LLM requests, full errors", **kw)
    p.add_argument("--no-run-log", action="store_false", dest="run_log",
                   help="do not write runs/_logs/<time>-<command>.log",
                   **({"default": True} if defaults else kw))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    common = argparse.ArgumentParser(add_help=False)
    _observe_flags(common, defaults=False)            # usable after the command name too
    ap = argparse.ArgumentParser(prog="cexgen")
    _observe_flags(ap, defaults=True)
    sub = ap.add_subparsers(dest="command", required=True)
    _add = sub.add_parser
    sub.add_parser = lambda *a, **k: _add(*a, parents=[common], **k)   # noqa: E731

    p = sub.add_parser("check", help="run the whole pipeline (Steps 1-9) and show each result and hand-off")
    p.add_argument("path", help="a case folder, a batch .json, a VeriEQL .jsonlines, or a folder of cases")
    _selection(p)
    p.add_argument("--keep-db", action="store_true", help="keep the sandbox database for inspection")
    p.add_argument("--fresh", action="store_true", help="ignore saved schema memory (clean evaluation run)")
    p.add_argument("--runs-dir", help="where attempt logs and schema memory go (default: runs/ or $CEX_RUNS_DIR)")
    p.add_argument("--no-llm", action="store_true", help="baseline mode: the rule table instead of the LLM")
    p.set_defaults(func=_check)

    p = sub.add_parser("schema", help="show the schema model (Step 2) for the case(s) under PATH")
    p.add_argument("path", help="a case folder, a batch .json, a VeriEQL .jsonlines, or a folder of cases")
    _selection(p)
    p.add_argument("--json", action="store_true", help="print the full model as JSON")
    p.set_defaults(func=_schema)

    p = sub.add_parser("queries", help="show what Step 3 found in Q1 / Q2 (and the schema's rules)")
    p.add_argument("path", help="a case folder, a batch .json, a VeriEQL .jsonlines, or a folder of cases")
    _selection(p)
    p.add_argument("--rules", action="store_true", help="also show how the schema's CHECK / index rules were read")
    p.add_argument("--runs-dir", help="where attempt logs go (default: runs/ or $CEX_RUNS_DIR)")
    p.set_defaults(func=_queries)

    p = sub.add_parser("plan", help="show the insert plan (Step 4): table order and how each FK is satisfied")
    p.add_argument("path", help="a case folder, a batch .json, a VeriEQL .jsonlines, or a folder of cases")
    _selection(p)
    p.add_argument("--runs-dir", help="where attempt logs go (default: runs/ or $CEX_RUNS_DIR)")
    p.set_defaults(func=_plan)

    p = sub.add_parser("base", help="show the base data (Step 5): one row per table, and where each value came from")
    p.add_argument("path", help="a case folder, a batch .json, a VeriEQL .jsonlines, or a folder of cases")
    _selection(p)
    p.add_argument("--no-llm", action="store_true", help="baseline mode: the rule table instead of the LLM")
    p.add_argument("--fresh", action="store_true", help="ignore saved schema memory")
    p.add_argument("--runs-dir", help="where attempt logs and schema memory go (default: runs/ or $CEX_RUNS_DIR)")
    p.set_defaults(func=_base)

    p = sub.add_parser("load", help="run Steps 1-6 and show what was inserted and repaired")
    p.add_argument("path", help="a case folder, a batch .json, a VeriEQL .jsonlines, or a folder of cases")
    _selection(p)
    p.add_argument("--no-llm", action="store_true", help="baseline mode: the rule table instead of the LLM")
    p.add_argument("--fresh", action="store_true", help="ignore saved schema memory")
    p.add_argument("--runs-dir", help="where attempt logs and schema memory go (default: runs/ or $CEX_RUNS_DIR)")
    p.set_defaults(func=_load)

    p = sub.add_parser("logs", help="show the newest run log (or --list them, or --clean old ones)")
    p.add_argument("--list", action="store_true", help="list the run logs, newest first")
    p.add_argument("--clean", type=int, metavar="DAYS", help="delete run logs older than DAYS days")
    p.add_argument("--runs-dir", help="where the runs are (default: runs/ or $CEX_RUNS_DIR)")
    p.set_defaults(func=_logs)          # (main never writes a run log for `logs` itself)

    p = sub.add_parser("cleanup", help="drop leftover sandbox databases")
    p.add_argument("--older-than", type=int, default=60, metavar="MINUTES")
    p.set_defaults(func=_cleanup)

    args = ap.parse_args(argv)
    session = Session(args.command, argv, _effective_settings(args), args.verbosity,
                      args.run_log and args.command != "logs")
    log = logging.getLogger("cexgen.cli")
    code, crash = 1, None
    try:
        log.info("cexgen %s started", args.command)
        code = args.func(args)
    except CexError as e:
        log.error("%s", str(e).splitlines()[0])
        print(f"error: {e}", file=sys.stderr)
        code = e.exit_code
    except KeyboardInterrupt:
        log.warning("interrupted")
        print("interrupted", file=sys.stderr)
        code = 130
    except Exception as e:                             # a bug: keep the traceback in the run log
        log.exception("cexgen crashed")
        print(f"cexgen crashed: {type(e).__name__}: {e}", file=sys.stderr)
        code, crash = 1, e
    finally:
        session.finish(code, crash)
        if session.path is not None:
            print(f"run log: {session.path}", file=sys.stderr)
    return code


def _effective_settings(args):
    """The settings the command will run with (for the run log's header)."""
    changes = {}
    if getattr(args, "runs_dir", None):
        changes["runs_dir"] = args.runs_dir
    if getattr(args, "no_llm", False):
        changes["llm_enabled"] = False
    if getattr(args, "fresh", False):
        changes["use_schema_memory"] = False
    return get_settings().with_(**changes)


def _logs(args) -> int:
    folder = Path(_effective_settings(args).runs_dir) / "_logs"
    logs = sorted(folder.glob("*.log"), reverse=True) if folder.is_dir() else []
    if args.clean is not None:
        cutoff = datetime.now() - timedelta(days=args.clean)
        old = [p for p in logs if datetime.fromtimestamp(p.stat().st_mtime) < cutoff]
        for p in old:
            p.unlink()
        print(f"deleted {len(old)} run log(s) older than {args.clean} day(s)")
        return 0
    if not logs:
        print(f"no run logs in {folder}")
        return 0
    if args.list:
        for p in logs:
            print(f"{p}  ({p.stat().st_size // 1024} KB)")
        return 0
    print(f"== {logs[0]}\n")
    print(logs[0].read_text())
    return 0


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

    cases = load_cases(args.path, settings, args.sample, args.seed)
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
            for r in c.state.get("rounds", [])[1:]:
                print(f"         rebuild round {r.number} ({r.method}): {', '.join(r.changes) or 'no change'}"
                      f" -> {r.comparison.outcome.replace('_', ' ')}")
                for n in r.notes:
                    print(f"           note: {n}")
            comparison = c.state.get("comparison")
            if comparison is not None:
                print("         " + describe_comparison(comparison).replace("\n", "\n         "))
            doc = c.state.get("handoff")
            if doc is not None:
                verified = {True: "replay verified", False: "REPLAY DID NOT REPRODUCE",
                            None: "replay not verified"}[doc["data"]["replay_verified"]]
                print(f"         => {doc['status'].upper().replace('_', ' ')} ({verified})"
                      f"\n            hand-off: {c.state.get('handoff_path')}")
            if c.error and not run.error:
                print(f"         error: {c.error}")
            failed += c.status != "ok"
    return 1 if failed else 0


def _schema(args) -> int:
    settings = get_settings()
    cases = load_cases(args.path, settings, args.sample, args.seed)
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
    cases = load_cases(args.path, settings, args.sample, args.seed)
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
    for run in run_cases(load_cases(args.path, settings, args.sample, args.seed), settings, pipeline=[parse_schema, parse_queries, plan_order]):
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
    for run in run_cases(load_cases(args.path, settings, args.sample, args.seed), settings,
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
    for run in run_cases(load_cases(args.path, settings, args.sample, args.seed), settings,
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
