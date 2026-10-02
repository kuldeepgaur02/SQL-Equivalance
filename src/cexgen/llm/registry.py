"""Which LLM providers exist. To add an API:

    1. write a class with `name` and `complete_json(...)` (see base.LLMProvider),
       for example in llm/providers/my_api.py;
    2. register it:  register_provider("my_api", MyApiProvider)
    3. select it:    CEX_LLM_PROVIDER=my_api  (and CEX_LLM_MODEL=...)

Nothing else changes: the pipeline only talks to the provider interface.
"""
from __future__ import annotations

from typing import Callable

from ..config import Settings
from ..errors import LLMUnavailable
from .base import LLMProvider

_FACTORIES: dict[str, Callable[[Settings], LLMProvider]] = {}


def register_provider(name: str, factory: Callable[[Settings], LLMProvider]) -> None:
    _FACTORIES[name] = factory


def get_provider(settings: Settings) -> LLMProvider:
    factory = _FACTORIES.get(settings.llm_provider)
    if factory is None:
        raise LLMUnavailable(f"unknown LLM provider {settings.llm_provider!r}; "
                             f"known: {', '.join(sorted(_FACTORIES)) or '(none)'}")
    return factory(settings)


def _claude(settings: Settings) -> LLMProvider:
    from .providers.claude import ClaudeProvider       # imported lazily: only needed in LLM mode
    return ClaudeProvider(settings)


register_provider("claude", _claude)
