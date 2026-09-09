"""Provider adapters and the schema-constrained contract (§7.4).

Recorded shapes, not live calls: the contract under test is ours, not the
vendor's uptime.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from pydantic import BaseModel

from app.adapters.http import HttpClient
from app.adapters.llm import (
    FallbackChain,
    GeminiProvider,
    GroqProvider,
    OllamaProvider,
    SchemaViolationError,
    complete_schema,
)
from app.adapters.llm.base import LLMError, LLMResult, ProviderRateLimited

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


class Answer(BaseModel):
    name: str
    years: int


@pytest.fixture
def http() -> HttpClient:
    return HttpClient(
        user_agent="CareerPilotBot/test",
        per_host_min_interval_seconds=0.0,
        max_retries=1,
        sleep=lambda _: None,
    )


def groq_response(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "llama-3.1-8b-instant",
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30},
        },
    )


@respx.mock
def test_groq_sends_temperature_zero_and_json_mode(http: HttpClient) -> None:
    """Every number in this system must be reproducible (§7.4)."""
    route = respx.post(GROQ_URL).mock(return_value=groq_response('{"name":"Ahmed","years":5}'))
    provider = GroqProvider(http, "test-key")
    result = provider.chat(system="s", user="u", model="llama-3.1-8b-instant")

    request = json.loads(route.calls[0].request.content)
    assert request["temperature"] == 0
    assert request["response_format"] == {"type": "json_object"}
    assert route.calls[0].request.headers["authorization"] == "Bearer test-key"
    assert result.tokens_in == 120 and result.tokens_out == 30


@respx.mock
def test_groq_rate_limit_carries_retry_after(http: HttpClient) -> None:
    respx.post(GROQ_URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "42"}))
    with pytest.raises(ProviderRateLimited) as exc:
        GroqProvider(http, "k").chat(system="s", user="u", model="m")
    assert exc.value.retry_after == 42.0


def test_groq_without_a_key_fails_immediately(http: HttpClient) -> None:
    with pytest.raises(LLMError, match="GROQ_API_KEY"):
        GroqProvider(http, "")


@respx.mock
def test_markdown_fenced_json_is_parsed() -> None:
    """Providers add fences even in JSON mode."""

    class Fenced:
        name = "fenced"

        def chat(self, **kwargs: object) -> LLMResult:
            return LLMResult(
                content='Here you go:\n```json\n{"name":"Ahmed","years":5}\n```',
                model="m",
                provider="fenced",
            )

    answer, _ = complete_schema(Fenced(), system="s", user="u", model="m", schema=Answer)
    assert answer.name == "Ahmed"


def test_invalid_output_gets_exactly_one_repair_attempt() -> None:
    """One repair, then hard failure: a third attempt spends a rate-limited
    budget worse than an honest error does (§7.4)."""

    class Flaky:
        name = "flaky"

        def __init__(self) -> None:
            self.calls: list[str] = []

        def chat(self, *, system: str, user: str, model: str, **kwargs: object) -> LLMResult:
            self.calls.append(user)
            content = '{"name":"Ahmed","years":5}' if len(self.calls) > 1 else '{"name":"Ahmed"}'
            return LLMResult(content=content, model=model, provider=self.name)

    provider = Flaky()
    answer, result = complete_schema(provider, system="s", user="u", model="m", schema=Answer)

    assert answer.years == 5
    assert len(provider.calls) == 2
    assert result.repaired and result.attempts == 2
    # The repair prompt shows the model its own output and the validation error.
    assert "did not satisfy the required JSON schema" in provider.calls[1]


def test_two_failures_raise_rather_than_guess() -> None:
    class Broken:
        name = "broken"

        def chat(self, **kwargs: object) -> LLMResult:
            return LLMResult(content="I cannot help with that.", model="m", provider="broken")

    with pytest.raises(SchemaViolationError) as exc:
        complete_schema(Broken(), system="s", user="u", model="m", schema=Answer)
    assert "I cannot help" in exc.value.raw


@respx.mock
def test_chain_falls_through_on_rate_limit(http: HttpClient) -> None:
    """A rate-limited provider hands over rather than retrying into a hard cap (R1)."""
    respx.post(GROQ_URL).mock(return_value=httpx.Response(429))
    respx.post(url__startswith="https://generativelanguage.googleapis.com").mock(
        return_value=httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": '{"name":"Ahmed","years":5}'}]}}],
                "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 20},
            },
        )
    )
    chain = FallbackChain([GroqProvider(http, "k"), GeminiProvider(http, "g")])
    result = chain.chat(system="s", user="u", model="llama-3.1-8b-instant")

    assert result.provider == "gemini"
    assert result.tokens_in == 100


@respx.mock
def test_chain_reports_every_failure_when_all_providers_fail(http: HttpClient) -> None:
    respx.post(GROQ_URL).mock(return_value=httpx.Response(500))
    respx.post(url__startswith="http://localhost:11434").mock(return_value=httpx.Response(500))
    chain = FallbackChain([GroqProvider(http, "k"), OllamaProvider(http)])
    with pytest.raises(LLMError, match="every provider failed"):
        chain.chat(system="s", user="u", model="m")


def test_chain_needs_at_least_one_provider() -> None:
    with pytest.raises(LLMError, match="GROQ_API_KEY"):
        FallbackChain([])


@respx.mock
def test_gemini_blocked_prompt_is_reported_clearly(http: HttpClient) -> None:
    respx.post(url__startswith="https://generativelanguage.googleapis.com").mock(
        return_value=httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})
    )
    with pytest.raises(LLMError, match="blockReason=SAFETY"):
        GeminiProvider(http, "g").chat(system="s", user="u", model="gemini-2.0-flash")
