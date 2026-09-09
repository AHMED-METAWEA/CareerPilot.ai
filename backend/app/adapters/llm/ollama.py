"""Ollama — local fallback (§7.1).

Slower and weaker than the hosted options, and subject to no quota at all. It
is what keeps the pipeline running when both free tiers are exhausted, and what
makes the system developable on a plane.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.adapters.http import HttpClient, RateLimitedError, SourceUnavailableError
from app.adapters.llm.base import ChatProvider, LLMError, LLMResult


class OllamaProvider(ChatProvider):
    name = "ollama"

    def __init__(self, http: HttpClient, base_url: str = "http://localhost:11434") -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")

    def chat(
        self, *, system: str, user: str, model: str, max_tokens: int = 2048, json_mode: bool = True
    ) -> LLMResult:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {"temperature": 0, "num_predict": max_tokens},
        }
        if json_mode:
            payload["format"] = "json"

        started = time.monotonic()
        try:
            response = self._http.request(
                "POST",
                f"{self._base_url}/api/chat",
                budget_key="llm:ollama",
                rate_limit_rpm=600,  # local: the constraint is the CPU, not a quota
                headers={"Content-Type": "application/json"},
                json=payload,
            )
        except (RateLimitedError, SourceUnavailableError, httpx.HTTPError) as exc:
            raise LLMError(f"ollama unreachable at {self._base_url}: {exc}") from exc

        if response.status_code >= 400:
            raise LLMError(f"ollama returned {response.status_code}: {response.text[:300]}")

        body = response.json()
        content = (body.get("message") or {}).get("content", "")
        if not content:
            raise LLMError(f"ollama returned no content: {str(body)[:200]}")

        return LLMResult(
            content=content,
            model=body.get("model", model),
            provider=self.name,
            tokens_in=int(body.get("prompt_eval_count", 0)),
            tokens_out=int(body.get("eval_count", 0)),
            latency_ms=(time.monotonic() - started) * 1000,
        )
