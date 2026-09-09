"""Language-model providers.

Groq is primary, Gemini and Ollama are fallbacks (§7.1). Model identifiers come
from `config/config.yaml`; none appears in this code, because vendors deprecate
names on their own schedule.
"""

from app.adapters.llm.base import (
    LLMError,
    LLMResult,
    ProviderRateLimited,
    SchemaViolationError,
    complete_schema,
)
from app.adapters.llm.chain import FallbackChain, build_provider
from app.adapters.llm.gemini import GeminiProvider
from app.adapters.llm.groq import GroqProvider
from app.adapters.llm.ollama import OllamaProvider

__all__ = [
    "FallbackChain",
    "GeminiProvider",
    "GroqProvider",
    "LLMError",
    "LLMResult",
    "OllamaProvider",
    "ProviderRateLimited",
    "SchemaViolationError",
    "build_provider",
    "complete_schema",
]
