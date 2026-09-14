"""Gemini — first fallback when Groq's limits bind (§7.1)."""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

import httpx

from app.adapters.http import HttpClient, RateLimitedError, SourceUnavailableError
from app.adapters.llm.base import (
    ChatProvider,
    LLMError,
    LLMResult,
    ProviderRateLimited,
    ProviderUnavailable,
)
from app.adapters.llm.keypool import AllKeysExhausted, ApiKeyPool

BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"


LLM_RETRY_WAIT_BUDGET = 8.0
"""Seconds of rate-limit backoff a provider will absorb before handing over.

The chain exists precisely so that a busy provider is not worth waiting for.
Groq's free tier answers 429 with `Retry-After` values up to forty-five seconds;
absorbing that inside the provider meant a matching run paid it on every
posting, while a configured fallback sat idle three seconds away."""


class GeminiProvider(ChatProvider):
    name = "gemini"

    def __init__(
        self,
        http: HttpClient,
        api_key: str | Sequence[str],
        *,
        base_url: str = BASE_URL,
    ) -> None:
        keys = [api_key] if isinstance(api_key, str) else list(api_key)
        if not any(key.strip() for key in keys if key):
            raise LLMError("GEMINI_API_KEY is not set")
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
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": max_tokens},
        }
        if json_mode:
            payload["generationConfig"]["responseMimeType"] = "application/json"

        started = time.monotonic()
        try:
            api_key = self._pool.acquire()
        except AllKeysExhausted as exc:
            raise ProviderRateLimited(f"gemini rate limited: {exc}", exc.retry_after) from exc

        try:
            response = self._http.request(
                "POST",
                f"{self._base_url}/{model}:generateContent",
                # Per key: a shared budget meters every key as one.
                budget_key=self._pool.budget_key(api_key),
                rate_limit_rpm=14,  # free tier is 15 rpm per key; a request of headroom
                headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
                json=payload,
                max_retry_wait=LLM_RETRY_WAIT_BUDGET,
            )
        except RateLimitedError as exc:
            self._pool.penalise(api_key, exc.retry_after)
            raise ProviderRateLimited(f"gemini rate limited: {exc}", exc.retry_after) from exc
        except (SourceUnavailableError, httpx.HTTPError) as exc:
            raise ProviderUnavailable(f"gemini unreachable: {exc}") from exc

        if response.status_code in (401, 403):
            # A refused credential is not a bad request, it is a provider that
            # will keep saying no until a human changes something. Reported as
            # unavailable so the chain parks it rather than paying for the same
            # refusal on every fallthrough — the same reasoning as a refused
            # connection, arriving over HTTP instead of TCP.
            raise ProviderUnavailable(
                f"gemini refused the credential ({response.status_code}): {response.text[:200]}"
            )
        if response.status_code >= 400:
            raise LLMError(f"gemini returned {response.status_code}: {response.text[:300]}")

        body = response.json()
        usage = body.get("usageMetadata") or {}
        try:
            parts = body["candidates"][0]["content"]["parts"]
            content = "".join(part.get("text", "") for part in parts)
        except (KeyError, IndexError) as exc:
            # A blocked prompt has no candidates; say so rather than crashing.
            reason = (body.get("promptFeedback") or {}).get("blockReason")
            raise LLMError(
                f"gemini returned no candidate (blockReason={reason}): {str(body)[:200]}"
            ) from exc

        return LLMResult(
            content=content,
            model=model,
            provider=self.name,
            tokens_in=int(usage.get("promptTokenCount", 0)),
            tokens_out=int(usage.get("candidatesTokenCount", 0)),
            latency_ms=(time.monotonic() - started) * 1000,
        )
