"""One line of the attempt log: what was done, and exactly what happened."""
from __future__ import annotations

import base64
import datetime as dt
import decimal
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

# Steps that write entries. Later steps add theirs here so the vocabulary stays in one place.
STEPS = ("run", "setup", "parse_schema", "parse_queries", "order", "base_data",
         "insert", "repair", "compare", "rebuild", "handoff")
STATUSES = ("ok", "failed", "skipped", "info")


@dataclass(frozen=True)
class Entry:
    seq: int                                   # 1, 2, 3 ... within one case
    case: str
    step: str                                  # one of STEPS
    action: str                                # short human-readable description
    status: str                                # one of STATUSES
    at: str                                    # UTC ISO-8601 time the action started
    duration_ms: float | None = None
    detail: dict[str, Any] = field(default_factory=dict)   # inputs / outputs, JSON data
    error: dict[str, Any] | None = None        # {"type", "message", "sqlstate"?, "constraint"?}
    llm: dict[str, Any] | None = None          # {"model", "input_tokens", "output_tokens", ...}

    def to_json(self) -> dict[str, Any]:
        return to_jsonable(asdict(self))

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Entry":
        return cls(**data)


def to_jsonable(value: Any) -> Any:
    """Database and Python values -> plain JSON, without losing information.
    Decimals and big integers become strings so no precision is lost."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value if abs(value) < 2 ** 53 else str(value)
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else str(value)
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"base64": base64.b64encode(bytes(value)).decode()}
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = sorted(value, key=repr) if isinstance(value, (set, frozenset)) else value
        return [to_jsonable(v) for v in items]
    return repr(value)
