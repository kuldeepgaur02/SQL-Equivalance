"""How one event is written, for the live console and the run log file alike."""
from __future__ import annotations

import datetime as dt
import json
from contextvars import ContextVar

from ..journal.entry import Entry

# The case being worked on, so lower-level log messages (sandbox, SQL, LLM) can say which case they belong to.
CURRENT_CASE: ContextVar[str] = ContextVar("cexgen_case", default="-")

STATUS_WORD = {"ok": "ok", "failed": "FAILED", "skipped": "skipped", "info": "info"}


def clock() -> str:
    return dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]


def headline(case: str, step: str, text: str, status: str = "", extra: str = "", width: int = 64) -> str:
    dots = "." * max(2, width - len(text)) if status else ""
    return f"{clock()}  {case[:26]:26}  {step:13}  {text} {dots} {status}{('  ' + extra) if extra else ''}".rstrip()


def entry_headline(entry: Entry) -> str:
    return headline(entry.case, entry.step, entry.action, STATUS_WORD.get(entry.status, entry.status), _extra(entry))


def _extra(entry: Entry) -> str:
    bits = []
    if entry.duration_ms is not None:
        bits.append(f"{entry.duration_ms:.0f} ms")
    if entry.error:
        code = entry.error.get("sqlstate")
        where = entry.error.get("constraint") or entry.error.get("table") or ""
        message = (entry.error.get("message") or "").splitlines()[0][:140]
        bits.append(" ".join(x for x in (code, where, message) if x))
    if entry.llm:
        tokens = [f"{entry.llm[k]} {k.split('_')[0]}" for k in ("input_tokens", "output_tokens") if k in entry.llm]
        bits.append(f"LLM {entry.llm.get('model', '')} {' / '.join(tokens)}".strip())
    detail = entry.detail or {}
    for key in ("outcome", "status", "method", "category"):
        if key in detail and detail[key] and entry.step in ("compare", "handoff", "repair", "rebuild"):
            bits.append(f"{key}={detail[key]}")
    if entry.step == "rebuild" and isinstance(detail.get("comparison"), dict):
        bits.append(f"outcome={detail['comparison'].get('outcome')}")
    return " | ".join(bits)


def entry_body(entry: Entry, indent: str = "      ") -> str:
    """Everything recorded for the entry: detail, error (with traceback), LLM call."""
    parts = {}
    if entry.detail:
        parts["detail"] = entry.detail
    if entry.error:
        parts["error"] = entry.error
    if entry.llm:
        parts["llm"] = entry.llm
    if not parts:
        return ""
    text = json.dumps(parts, indent=2, ensure_ascii=False, default=str)
    return "\n".join(indent + line for line in text.splitlines())
