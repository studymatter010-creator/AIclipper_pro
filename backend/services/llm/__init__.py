"""AIClipper LLM provider layer (BYOK).

Pluggable chat backends behind one common interface so each team *role* can be
served by Local (Ollama) or any configured API provider without the pipeline
code ever knowing which backend answered.
"""
from backend.services.llm.providers import (
    LLMProvider,
    OllamaProvider,
    OpenAIProvider,
    AnthropicProvider,
    GeminiProvider,
    OpenAICompatibleProvider,
    ProviderError,
    RateLimitError,
    build_provider,
    PROVIDER_BUILDERS,
)

__all__ = [
    "LLMProvider",
    "OllamaProvider",
    "OpenAIProvider",
    "AnthropicProvider",
    "GeminiProvider",
    "OpenAICompatibleProvider",
    "ProviderError",
    "RateLimitError",
    "build_provider",
    "PROVIDER_BUILDERS",
]
