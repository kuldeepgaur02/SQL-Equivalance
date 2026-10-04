"""The oracle: every decision the diagram gives to "LLM" goes through here.

  LLM mode (default)   ask the provider; if one call fails, use the rule table for
                       that decision and record the failure. Missing credentials
                       stop the case (LLMUnavailable): an "LLM" run is never
                       secretly a baseline run.
  --no-llm             the rule table only (baseline mode).

Answers that worked are cached in schema memory, so the same question on the
same schema is not asked (or paid for) twice.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from ..config import Settings
from ..journal import Journal
from ..memory import SchemaMemory
from . import prompts
from .base import LLMCallFailed, LLMProvider
from .registry import get_provider

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Answer:
    value: Any
    source: str          # "llm" | "memory" | "rule" | "rule (llm failed)" | "rule (llm answer invalid)"
    note: str = ""


class Oracle:
    def __init__(self, settings: Settings, journal: Journal, memory: SchemaMemory,
                 provider: LLMProvider | None = None):
        self.settings, self.journal, self.memory = settings, journal, memory
        self._provider = provider
        self.calls = 0

    @property
    def mode(self) -> str:
        return "llm" if self.settings.llm_enabled else "rules"

    def provider(self) -> LLMProvider:
        if self._provider is None:
            self._provider = get_provider(self.settings)
        return self._provider

    def ask(self, step: str, purpose: str, prompt: str, schema: dict) -> dict | None:
        """One LLM call, recorded in the attempt log. None if the call failed (caller uses rules)."""
        provider = self.provider()
        log.info("LLM request (%s / %s): %s, prompt %d characters", provider.name, self.settings.llm_model,
                 purpose, len(prompt))
        t0 = time.perf_counter()
        self.calls += 1
        llm_info = {"provider": provider.name, "model": self.settings.llm_model, "purpose": purpose}
        try:
            result = provider.complete_json(prompts.SYSTEM, prompt, schema, purpose)
        except LLMCallFailed as e:
            log.warning("LLM call failed (%s): %s; the rule table is used instead", purpose, e)
            self.journal.record(step, f"LLM: {purpose}", "failed", error={"type": "LLMCallFailed", "message": str(e)},
                                llm=llm_info, duration_ms=(time.perf_counter() - t0) * 1000,
                                detail={"fallback": "rule table", "prompt": prompt})
            return None
        llm_info.update(model=result.model, **result.usage)
        self.journal.record(step, f"LLM: {purpose}", "ok", llm=llm_info, duration_ms=(time.perf_counter() - t0) * 1000,
                            detail={"prompt": prompt, "answer": result.data})
        return result.data

    # -- schema-memory cache ---------------------------------------------------------
    @staticmethod
    def cache_key(*parts: Any) -> str:
        return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:20]

    def cached(self, namespace: str, key: str) -> Any:
        return self.memory.get(namespace, key) if self.settings.llm_enabled else None

    def remember(self, namespace: str, key: str, value: Any) -> None:
        self.memory.put(namespace, key, value)
