import pytest

from cexgen.config import Settings
from cexgen.errors import LLMUnavailable


def test_missing_credentials_become_llm_unavailable(monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    from cexgen.llm.providers.claude import ClaudeProvider

    provider = ClaudeProvider(Settings())
    with pytest.raises(LLMUnavailable, match="ANTHROPIC_API_KEY"):
        provider.complete_json("system", "prompt", {"type": "object", "properties": {}, "required": [],
                                                    "additionalProperties": False}, "test")


def test_default_model_is_configurable():
    assert Settings().llm_model == "claude-opus-5-5"
    assert Settings(llm_model="claude-sonnet-5-5").llm_model == "claude-sonnet-5-5"
