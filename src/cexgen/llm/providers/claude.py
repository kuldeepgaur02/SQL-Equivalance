"""Claude through the official Anthropic SDK.

Structured output (output_config.format) makes the answer valid JSON for the
given schema. Server-side fallback lets a request the model declines be
answered by another model instead of failing. Credentials come from
ANTHROPIC_API_KEY (or `ant auth login`).
"""
from __future__ import annotations

import json
from typing import Any

import anthropic

from ...config import Settings
from ...errors import LLMUnavailable
from ..base import LLMCallFailed, LLMResult

_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ClaudeProvider:
    name = "claude"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = anthropic.Anthropic(timeout=settings.llm_timeout_s, max_retries=2)

    def complete_json(self, system: str, prompt: str, schema: dict[str, Any], purpose: str) -> LLMResult:
        try:
            resp = self.client.beta.messages.create(
                model=self.settings.llm_model,
                max_tokens=self.settings.llm_max_tokens,
                betas=[_FALLBACK_BETA],
                fallbacks="default",
                output_config={"effort": self.settings.llm_effort,
                               "format": {"type": "json_schema", "schema": schema}},
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
        except TypeError as e:                      # the SDK raises this when no credentials are configured
            if "authentication" in str(e).lower():
                raise LLMUnavailable("no Claude credentials: set ANTHROPIC_API_KEY (or run `ant auth login`), "
                                     "or use --no-llm for the rule-table baseline") from None
            raise
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            raise LLMUnavailable(f"Claude rejected the credentials ({e.status_code}); "
                                 f"check ANTHROPIC_API_KEY, or use --no-llm") from None
        except anthropic.NotFoundError as e:
            raise LLMUnavailable(f"model {self.settings.llm_model!r} not available ({e.status_code}); "
                                 f"set CEX_LLM_MODEL") from None
        except anthropic.RateLimitError as e:
            raise LLMCallFailed(f"rate limited ({e.status_code})") from None
        except anthropic.APIStatusError as e:
            raise LLMCallFailed(f"API error {e.status_code}: {getattr(e, 'type', '') or e.message}") from None
        except anthropic.APIConnectionError as e:
            raise LLMCallFailed(f"connection error: {e}") from None

        usage = {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}
        if resp.stop_reason == "refusal":
            raise LLMCallFailed(f"the model declined ({getattr(resp, 'stop_details', None)})")
        if resp.stop_reason == "max_tokens":
            raise LLMCallFailed("the answer was cut off at max_tokens")
        text = next((b.text for b in resp.content if b.type == "text"), None)
        try:
            data = json.loads(text) if text else None
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise LLMCallFailed("the answer was not a JSON object")
        return LLMResult(data, resp.model, usage)
