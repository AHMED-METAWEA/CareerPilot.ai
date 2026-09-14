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
from app.adapters.llm.base import (
    LLMError,
    LLMResult,
    ProviderRateLimited,
    ProviderUnavailable,
)

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


# ── Cooldown for unreachable providers (§7.1) ─────────────────────────


class _Unreachable:
    """A provider that is not listening, and is slow to say so."""

    name = "unreachable"

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, **_: object) -> LLMResult:
        self.calls += 1
        raise ProviderUnavailable("connection refused")


class _RateLimited:
    name = "ratelimited"

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, **_: object) -> LLMResult:
        self.calls += 1
        raise ProviderRateLimited("429")


class _Working:
    name = "working"

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, **kwargs: object) -> LLMResult:
        self.calls += 1
        return LLMResult(content="{}", model=str(kwargs.get("model")), provider=self.name)


def _chat(chain: FallbackChain) -> object:
    return chain.chat(system="s", user="u", model="m")


def test_an_unreachable_provider_is_skipped_after_the_first_failure() -> None:
    """A refused connection costs ~30s of HTTP retries, and under a rate-limited
    primary that was paid on every single call — twelve minutes in one run."""
    dead, working = _Unreachable(), _Working()
    chain = FallbackChain([dead, working], clock=lambda: 0.0)

    for _ in range(5):
        _chat(chain)

    assert dead.calls == 1, "the dead provider must be tried once, not five times"
    assert working.calls == 5


def test_the_cooldown_expires() -> None:
    """Short enough that a provider coming back is picked up quickly."""
    dead, working = _Unreachable(), _Working()
    now = 0.0
    chain = FallbackChain([dead, working], clock=lambda: now, cooldown_seconds=120.0)

    _chat(chain)
    assert dead.calls == 1
    now = 121.0
    _chat(chain)
    assert dead.calls == 2, "after the cooldown it is tried again"


def test_rate_limiting_does_not_trigger_a_cooldown() -> None:
    """ "Not now" is not "not here". Parking the primary over one burst would
    send every later request to a weaker model."""
    limited, working = _RateLimited(), _Working()
    chain = FallbackChain([limited, working], clock=lambda: 0.0)

    for _ in range(3):
        _chat(chain)

    assert limited.calls == 3, "the primary must keep being offered work"


def test_a_cooldown_never_empties_the_chain() -> None:
    """A transient outage must not make the system refuse work it could do."""
    dead = _Unreachable()
    chain = FallbackChain([dead], clock=lambda: 0.0)

    with pytest.raises(LLMError):
        _chat(chain)
    with pytest.raises(LLMError):
        _chat(chain)

    assert dead.calls == 2, "the only provider is still tried rather than skipped"


# ── Key rotation through the provider (§7.1) ──────────────────────────


@respx.mock
def test_groq_rotates_keys_and_meters_them_separately(http_client: HttpClient) -> None:
    """The provider must actually send different keys, and give each its own
    rate budget — sharing one budget would meter four keys as one."""
    seen: list[str] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}], "usage": {}})

    respx.post("https://api.groq.com/openai/v1/chat/completions").mock(side_effect=record)
    provider = GroqProvider(http_client, ["k1", "k2", "k3"])
    assert provider.keys == 3

    for _ in range(6):
        provider.chat(system="s", user="u", model="m")

    assert {h.removeprefix("Bearer ") for h in seen} == {"k1", "k2", "k3"}
    assert len({row["key"] for row in provider.key_stats()}) == 3


@respx.mock
def test_a_rate_limited_key_is_parked_and_the_next_call_uses_another(
    http_client: HttpClient,
) -> None:
    """One key hitting its ceiling must not take the pool down with it."""
    seen: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        key = request.headers["Authorization"].removeprefix("Bearer ")
        seen.append(key)
        if key == "k1":
            return httpx.Response(429, headers={"Retry-After": "30"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}], "usage": {}})

    respx.post("https://api.groq.com/openai/v1/chat/completions").mock(side_effect=respond)
    provider = GroqProvider(http_client, ["k1", "k2"])

    with pytest.raises(ProviderRateLimited):
        provider.chat(system="s", user="u", model="m")

    # k1 is now parked, so the next several calls must all land on k2.
    for _ in range(3):
        provider.chat(system="s", user="u", model="m")
    assert seen[1:] == ["k2", "k2", "k2"]

    parked = next(row for row in provider.key_stats() if row["rate_limits"] == 1)
    assert parked["cooling_down_for"] > 0


@respx.mock
def test_a_refused_credential_is_reported_as_unavailable(http_client: HttpClient) -> None:
    """A 403 is not a bad request — it is a provider that will keep saying no
    until a human changes something. Reported as unavailable so the chain parks
    it instead of paying for the same refusal on every fallthrough.
    """
    respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(403, text='{"error":{"message":"denied"}}')
    )
    provider = GroqProvider(http_client, "k")

    with pytest.raises(ProviderUnavailable, match="refused the credential"):
        provider.chat(system="s", user="u", model="m")


@respx.mock
def test_a_denied_provider_is_only_asked_once_per_cooldown(http_client: HttpClient) -> None:
    """The observed case: a Gemini project denied server-side overnight, and
    every rate-limited Groq call then paid a round trip to be refused again."""
    denied = respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(403, text="denied")
    )
    working = _Working()
    chain = FallbackChain([GroqProvider(http_client, "k"), working], clock=lambda: 0.0)

    for _ in range(4):
        chain.chat(system="s", user="u", model="m")

    assert denied.call_count == 1, "asked once, then parked"
    assert working.calls == 4
