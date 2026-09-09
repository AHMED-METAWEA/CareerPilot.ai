"""Provider fallback chain (§7.1, R1).

Groq first, then whatever `models.fallbacks` lists. A provider that is rate
limited hands over to the next one rather than retrying into a hard cap — the
nightly matching run has a token budget, and burning it on retries is how a run
fails at 02:00 with nothing to show.

A provider that fails for any other reason also hands over, but the failure is
logged at error level: an unreachable Groq is an incident, a rate-limited Groq
is Tuesday.
"""

from __future__ import annotations

import structlog

from app.adapters.http import HttpClient
from app.adapters.llm.base import (
    ChatProvider,
    LLMError,
    LLMResult,
    ProviderRateLimited,
)
from app.adapters.llm.gemini import GeminiProvider
from app.adapters.llm.groq import GroqProvider
from app.adapters.llm.ollama import OllamaProvider
from app.config import Settings

log = structlog.get_logger(__name__)


class FallbackChain(ChatProvider):
    """Tries each provider in order. The first to answer wins."""

    name = "chain"

    def __init__(self, providers: list[ChatProvider]) -> None:
        if not providers:
            raise LLMError("no inference provider is configured; set GROQ_API_KEY")
        self._providers = providers

    @property
    def primary(self) -> ChatProvider:
        return self._providers[0]

    def chat(
        self, *, system: str, user: str, model: str, max_tokens: int = 2048, json_mode: bool = True
    ) -> LLMResult:
        errors: list[str] = []
        for index, provider in enumerate(self._providers):
            try:
                return provider.chat(
                    system=system,
                    user=user,
                    # Only the primary provider's model identifier is meaningful;
                    # a fallback runs whatever it was configured with.
                    model=model if index == 0 else _fallback_model(provider, model),
                    max_tokens=max_tokens,
                    json_mode=json_mode,
                )
            except ProviderRateLimited as exc:
                log.warning("llm.rate_limited", provider=provider.name, retry_after=exc.retry_after)
                errors.append(f"{provider.name}: rate limited")
            except LLMError as exc:
                log.error("llm.provider_failed", provider=provider.name, error=str(exc))
                errors.append(f"{provider.name}: {exc}")
        raise LLMError(f"every provider failed: {'; '.join(errors)}")


_FALLBACK_MODELS = {
    "gemini": "gemini-2.0-flash",
    "ollama": "llama3.1:8b",
}


def _fallback_model(provider: ChatProvider, requested: str) -> str:
    """A fallback provider cannot serve the primary's model identifier."""
    return _FALLBACK_MODELS.get(provider.name, requested)


def build_provider(settings: Settings, http: HttpClient, fallbacks: list[str]) -> FallbackChain:
    """Assemble the chain from configuration and whatever credentials exist.

    A provider with no credential is skipped rather than added as a guaranteed
    failure, so a deployment with only a Groq key gets a one-provider chain and
    a clear error if that key is missing too.
    """
    providers: list[ChatProvider] = []
    if settings.groq_api_key:
        providers.append(GroqProvider(http, settings.groq_api_key))

    for name in fallbacks:
        if name == "gemini" and settings.gemini_api_key:
            providers.append(GeminiProvider(http, settings.gemini_api_key))
        elif name == "ollama":
            providers.append(OllamaProvider(http, settings.ollama_base_url))

    return FallbackChain(providers)
