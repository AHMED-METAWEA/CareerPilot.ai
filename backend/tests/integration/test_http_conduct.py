"""Crawling conduct (§5.6).

These rules are the difference between a system that is welcome on an origin
and one that gets blocked. They live in one place so no adapter can forget
them, and they are tested here so the promise in docs/DATA_SOURCES.md is not
just prose.
"""

from __future__ import annotations

import time
from datetime import UTC

import httpx
import pytest
import respx

from app.adapters.http import HttpClient, RateLimitedError, RobotsCache, SourceUnavailableError


@pytest.fixture
def slept() -> list[float]:
    return []


@pytest.fixture
def client(slept: list[float]) -> HttpClient:
    """A client whose backoff is recorded rather than waited out."""
    return HttpClient(
        user_agent="CareerPilotBot/0.1 (+https://careerpilot.ai/bot; contact@careerpilot.ai)",
        max_retries=3,
        per_host_min_interval_seconds=0.0,
        sleep=slept.append,
    )


@respx.mock
def test_descriptive_user_agent_with_contact_is_sent(client: HttpClient) -> None:
    route = respx.get("https://example.com/jobs").mock(return_value=httpx.Response(200, json=[]))
    client.get_json("https://example.com/jobs")
    agent = route.calls[0].request.headers["user-agent"]
    assert "CareerPilotBot" in agent and "http" in agent  # identifies us, and how to reach us


@respx.mock
def test_conditional_request_headers_round_trip(client: HttpClient) -> None:
    """ETag in, If-None-Match out: bandwidth neither side needs to spend (§5.6)."""
    from app.adapters.http import ConditionalState

    state = ConditionalState()
    responses = [
        httpx.Response(
            200,
            json=[],
            headers={"ETag": "W/abc", "Last-Modified": "Tue, 01 Sep 2026 10:00:00 GMT"},
        ),
        httpx.Response(304),
    ]
    route = respx.get("https://example.com/jobs").mock(side_effect=responses)

    client.get_json("https://example.com/jobs", conditional=state)
    assert state.etag == "W/abc"
    assert state.last_modified == "Tue, 01 Sep 2026 10:00:00 GMT"

    assert client.get_json("https://example.com/jobs", conditional=state) is None
    second = route.calls[1].request
    assert second.headers["if-none-match"] == "W/abc"
    assert second.headers["if-modified-since"] == "Tue, 01 Sep 2026 10:00:00 GMT"


@respx.mock
def test_429_honours_retry_after_then_raises(client: HttpClient, slept: list[float]) -> None:
    respx.get("https://example.com/jobs").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "30"})
    )
    with pytest.raises(RateLimitedError) as exc:
        client.get_json("https://example.com/jobs")

    # Waited the interval the origin asked for, not a schedule of our own.
    assert slept == [30.0, 30.0]
    # The worker then parks the source for this long rather than retrying into a quota.
    assert exc.value.retry_after == 30.0


@respx.mock
def test_retry_after_as_http_date_is_understood(client: HttpClient) -> None:
    from datetime import datetime, timedelta
    from email.utils import format_datetime

    when = datetime.now(UTC) + timedelta(seconds=45)
    respx.get("https://example.com/jobs").mock(
        return_value=httpx.Response(503, headers={"Retry-After": format_datetime(when)})
    )
    with pytest.raises(RateLimitedError) as exc:
        client.get_json("https://example.com/jobs")
    assert exc.value.retry_after is not None and 40 <= exc.value.retry_after <= 46


@respx.mock
def test_backoff_grows_between_attempts(client: HttpClient, slept: list[float]) -> None:
    respx.get("https://example.com/jobs").mock(return_value=httpx.Response(500))
    with pytest.raises(SourceUnavailableError):
        client.get_json("https://example.com/jobs")
    assert slept == sorted(slept) and len(set(slept)) > 1


@respx.mock
def test_5xx_is_retried_then_reported_as_unavailable(client: HttpClient) -> None:
    route = respx.get("https://example.com/jobs").mock(return_value=httpx.Response(503))
    with pytest.raises((SourceUnavailableError, RateLimitedError)):
        client.get_json("https://example.com/jobs")
    assert route.call_count > 1  # retried, not abandoned on the first failure


def test_per_host_interval_is_enforced() -> None:
    """Per-host concurrency of 1 with a minimum interval, whatever the source rpm."""
    throttled = HttpClient(user_agent="test", per_host_min_interval_seconds=0.25)
    with respx.mock:
        respx.get("https://example.com/a").mock(return_value=httpx.Response(200, json=[]))
        started = time.monotonic()
        for _ in range(3):
            throttled.get_json("https://example.com/a", rate_limit_rpm=6000)
        elapsed = time.monotonic() - started
    throttled.close()
    assert elapsed >= 0.5  # two gaps of 0.25s between three requests


@respx.mock
def test_robots_disallow_is_honoured(client: HttpClient) -> None:
    respx.get("https://example.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
    )
    robots = RobotsCache(client)
    assert robots.allowed("https://example.com/jobs/1") is True
    assert robots.allowed("https://example.com/private/1") is False


@respx.mock
def test_missing_robots_means_unrestricted(client: HttpClient) -> None:
    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
    assert RobotsCache(client).allowed("https://example.com/anything") is True


@respx.mock
def test_unreachable_robots_is_treated_as_disallow(client: HttpClient) -> None:
    """RFC 9309: a 5xx on robots.txt means 'do not crawl', not 'crawl freely'."""
    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(500))
    assert RobotsCache(client).allowed("https://example.com/jobs/1") is False


@respx.mock
def test_robots_is_fetched_once_and_cached(client: HttpClient) -> None:
    route = respx.get("https://example.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nAllow: /\n")
    )
    robots = RobotsCache(client)
    for _ in range(5):
        robots.allowed("https://example.com/jobs/1")
    assert route.call_count == 1


@respx.mock
def test_a_rate_limit_longer_than_the_budget_hands_over_rather_than_waits(
    http_client: HttpClient,
) -> None:
    """Honouring a long `Retry-After` is right when this client is the only way
    to the data. Behind a fallback chain it is not: Groq's free tier asks for up
    to forty-five seconds, and absorbing that inside the provider meant a
    matching run paid it on every posting while a configured fallback sat idle
    three seconds away.
    """
    route = respx.get("https://example.test/slow").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "45"})
    )

    with pytest.raises(RateLimitedError) as exc:
        http_client.request("GET", "https://example.test/slow", max_retry_wait=8.0)

    assert route.call_count == 1, "it must not sleep and retry past the budget"
    assert exc.value.retry_after == 45.0, "the caller still learns how long was asked for"


@respx.mock
def test_a_rate_limit_inside_the_budget_is_still_waited_out(
    http_client: HttpClient,
) -> None:
    """The budget is a ceiling, not a refusal to retry at all."""
    route = respx.get("https://example.test/brief").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "1"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )

    response = http_client.request("GET", "https://example.test/brief", max_retry_wait=8.0)

    assert response.status_code == 200
    assert route.call_count == 2


@respx.mock
def test_without_a_budget_a_long_retry_after_is_still_honoured(
    http_client: HttpClient,
) -> None:
    """ATS boards have no fallback, so the old behaviour has to survive."""
    route = respx.get("https://example.test/board").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "30"}),
            httpx.Response(200, json={"jobs": []}),
        ]
    )

    response = http_client.request("GET", "https://example.test/board")

    assert response.status_code == 200
    assert route.call_count == 2
