"""Gemini — first fallback when Groq's limits bind (§7.1)."""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.adapters.http import HttpClient, RateLimitedError, SourceUnavailableError
from app.adapters.llm.base import ChatProvider, LLMError, LLMResult, ProviderRateLimited

BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiProvider(ChatProvider):
    name = "gemini"

    def __init__(self, http: HttpClient, api_key: str, *, base_url: str = BASE_URL) -> None:
        if not api_key:
            raise LLMError("GEMINI_API_KEY is not set")
        self._http = http
        self._api_key = api_key
        self._base_url = base_url

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
            response = self._http.request(
                "POST",
                f"{self._base_url}/{model}:generateContent",
                budget_key="llm:gemini",
                rate_limit_rpm=14,  # free tier is 15 rpm; leave a request of headroom
                headers={"Content-Type": "application/json", "x-goog-api-key": self._api_key},
                json=payload,
            )
        except RateLimitedError as exc:
            raise ProviderRateLimited(f"gemini rate limited: {exc}", exc.retry_after) from exc
        except (SourceUnavailableError, httpx.HTTPError) as exc:
            raise LLMError(f"gemini unreachable: {exc}") from exc

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
