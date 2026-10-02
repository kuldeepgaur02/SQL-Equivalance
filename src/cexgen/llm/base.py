"""The interface every LLM provider implements.

A provider does one thing: given a system prompt, a task prompt and a JSON
Schema, it returns JSON that follows the schema. Everything cexgen asks an LLM
(JSON/ARRAY values, repairs, rebuilds) goes through this one method, so adding
another API means writing one class (see providers/claude.py) and registering it
(registry.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class LLMResult:
    data: dict[str, Any]              # the parsed JSON answer, already shaped by the schema
    model: str                        # the model that actually answered
    usage: dict[str, Any] = field(default_factory=dict)   # tokens etc., recorded in the attempt log


class LLMCallFailed(Exception):
    """One call failed (rate limit, server error, refusal, bad output). The caller
    falls back to the rule table for that decision and records it."""


class LLMProvider(Protocol):
    name: str

    def complete_json(self, system: str, prompt: str, schema: dict[str, Any], purpose: str) -> LLMResult:
        """Return JSON matching `schema`. Raise LLMCallFailed for a failed call and
        errors.LLMUnavailable when the provider cannot be used at all (e.g. no credentials)."""
        ...
