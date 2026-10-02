"""The LLM, behind a provider interface (see registry.py to add another API)."""
from .base import LLMCallFailed, LLMProvider, LLMResult
from .oracle import Answer, Oracle
from .registry import get_provider, register_provider

__all__ = ["LLMProvider", "LLMResult", "LLMCallFailed", "Oracle", "Answer", "get_provider", "register_provider"]
