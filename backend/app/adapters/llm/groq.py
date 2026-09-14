"""Groq — primary provider (§7.1).

OpenAI-compatible chat completions. Called over the shared polite HTTP client
rather than a vendor SDK: one fewer dependency, one fewer place for retry
behaviour to diverge, and `respx` can record it like any other source.

The free tier is roughly 30 requests and 6,000 tokens per minute at the
organisation level, which is the entire reason the funnel in §4.2 exists.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

import httpx
import structlog

from app.adapters.http import HttpClient, RateLimitedError, SourceUnavailableError
from app.adapters.llm.base import (
    ChatProvider,
    LLMError,
    LLMResult,
    ProviderRateLimited,
    ProviderUnavailable,
)
from app.adapters.llm.keypool import AllKeysExhausted, ApiKeyPool

log = structlog.get_logger(__name__)

BASE_URL = "https://api.groq.com/openai/v1/chat/completions"


LLM_RETRY_WAIT_BUDGET = 8.0
"""Seconds of rate-limit backoff a provider will absorb before handing over.

The chain exists precisely so that a busy provider is not worth waiting for.
Groq's free tier answers 429 with `Retry-After` values up to forty-five seconds;
absorbing that inside the provider meant a matching run paid it on every
posting, while a configured fallback sat idle three seconds away."""


class GroqProvider(ChatProvider):
    name = "groq"

    def __init__(
        self,
        http: HttpClient,
        api_key: str | Sequence[str],
        *,
        base_url: str = BASE_URL,
    ) -> None:
        keys = [api_key] if isinstance(api_key, str) else list(api_key)
        if not any(key.strip() for key in keys if key):
            raise LLMError("GROQ_API_KEY is not set")
        self._http = http
        self._pool = ApiKeyPool(keys, provider=self.name)
        self._base_url = base_url

    @property
    def keys(self) -> int:
        return len(self._pool)

    def key_stats(self) -> list[dict[str, object]]:
        return self._pool.stats()

    def chat(
        self, *, system: str, user: str, model: str, max_tokens: int = 2048, json_mode: bool = True
    ) -> LLMResult:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            # Temperature 0: every number in this system must be reproducible,
            # and an extraction that varies between runs is not evidence.
            "temperature": 0,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        started = time.monotonic()
        try:
            api_key = self._pool.acquire()
        except AllKeysExhausted as exc:
            raise ProviderRateLimited(f"groq rate limited: {exc}", exc.retry_after) from exc

        try:
            response = self._http.request(
                "POST",
                self._base_url,
                # Per key: a shared budget would meter every key as one, and
                # holding four keys would buy no extra throughput at all.
                budget_key=self._pool.budget_key(api_key),
                rate_limit_rpm=25,  # under the ~30 rpm free-tier ceiling, per key
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                max_retry_wait=LLM_RETRY_WAIT_BUDGET,
            )
        except RateLimitedError as exc:
            # Park this key rather than the provider: the others may be fine,
            # and the next call should reach one of them.
            self._pool.penalise(api_key, exc.retry_after)
            raise ProviderRateLimited(f"groq rate limited: {exc}", exc.retry_after) from exc
        except (SourceUnavailableError, httpx.HTTPError) as exc:
            raise ProviderUnavailable(f"groq unreachable: {exc}") from exc

        if response.status_code >= 400:
            raise LLMError(f"groq returned {response.status_code}: {response.text[:300]}")

        body = response.json()
        usage = body.get("usage") or {}
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"unexpected groq response shape: {str(body)[:300]}") from exc

        return LLMResult(
            content=content,
            model=body.get("model", model),
            provider=self.name,
            tokens_in=int(usage.get("prompt_tokens", 0)),
            tokens_out=int(usage.get("completion_tokens", 0)),
            latency_ms=(time.monotonic() - started) * 1000,
        )
