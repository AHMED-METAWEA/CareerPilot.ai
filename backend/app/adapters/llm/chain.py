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

import time
from collections.abc import Callable

import structlog

from app.adapters.http import HttpClient
from app.adapters.llm.base import (
    ChatProvider,
    LLMError,
    LLMResult,
    ProviderRateLimited,
    ProviderUnavailable,
)
from app.adapters.llm.gemini import GeminiProvider
from app.adapters.llm.groq import GroqProvider
from app.adapters.llm.ollama import OllamaProvider
from app.config import Settings

log = structlog.get_logger(__name__)


UNAVAILABLE_COOLDOWN_SECONDS = 120.0
"""How long to stop calling a provider that refused a connection.

A configured-but-absent fallback is the normal case in development, and it is
expensive: the HTTP client retries a refused connection three times with
exponential backoff, so every call to a dead local Ollama costs about thirty
seconds. Under a rate-limited primary that happens on *every* request — a single
matching run spent roughly twelve minutes waiting on a port with nothing behind
it.

Two minutes is short enough that a provider coming back is picked up quickly,
and long enough that a batch of requests pays the discovery cost once."""


class FallbackChain(ChatProvider):
    """Tries each provider in order. The first to answer wins."""

    name = "chain"

    def __init__(
        self,
        providers: list[ChatProvider],
        *,
        clock: Callable[[], float] = time.monotonic,
        cooldown_seconds: float = UNAVAILABLE_COOLDOWN_SECONDS,
    ) -> None:
        if not providers:
            raise LLMError("no inference provider is configured; set GROQ_API_KEY")
        self._providers = providers
        self._clock = clock
        self._cooldown_seconds = cooldown_seconds
        self._unavailable_until: dict[str, float] = {}

    @property
    def primary(self) -> ChatProvider:
        return self._providers[0]

    def _is_cooling_down(self, provider: ChatProvider) -> bool:
        until = self._unavailable_until.get(provider.name)
        return until is not None and self._clock() < until

    def chat(
        self, *, system: str, user: str, model: str, max_tokens: int = 2048, json_mode: bool = True
    ) -> LLMResult:
        errors: list[str] = []
        skipped: list[str] = []

        for index, provider in enumerate(self._providers):
            # Skipping is never allowed to empty the chain: if every provider is
            # cooling down, the last one is still tried, so a transient outage
            # cannot make the system refuse work it could have done.
            remaining = self._providers[index + 1 :]
            if self._is_cooling_down(provider) and (remaining or errors):
                skipped.append(provider.name)
                continue

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
                # Deliberately not a cooldown. Rate limiting says "not now", not
                # "not here", and parking the primary would send every request to
                # a weaker model for two minutes over one burst.
                log.warning("llm.rate_limited", provider=provider.name, retry_after=exc.retry_after)
                errors.append(f"{provider.name}: rate limited")
            except ProviderUnavailable as exc:
                self._unavailable_until[provider.name] = self._clock() + self._cooldown_seconds
                log.warning(
                    "llm.provider_unavailable",
                    provider=provider.name,
                    cooldown_seconds=self._cooldown_seconds,
                    error=str(exc),
                )
                errors.append(f"{provider.name}: unavailable")
            except LLMError as exc:
                log.error("llm.provider_failed", provider=provider.name, error=str(exc))
                errors.append(f"{provider.name}: {exc}")

        if skipped:
            log.info("llm.providers_skipped", skipped=skipped, reason="cooling down")
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
