"""Polite HTTP client shared by every source adapter and the URL verifier.

The conduct rules of §5.6 are implemented here once, so that no adapter can
forget them: a descriptive User-Agent with a contact URL, per-host concurrency
of one with a minimum interval, exponential backoff that honours `Retry-After`,
conditional requests where the origin supports them, and no circumvention of
anything.
"""

from __future__ import annotations

import threading
import time
import urllib.robotparser
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
import structlog

log = structlog.get_logger(__name__)


class RateLimitedError(RuntimeError):
    """Raised when a source answers 429/503 and the retry budget is exhausted.

    Carries `retry_after` so the worker can park the whole source instead of
    hammering a quota it has already hit (§13.2)."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class SourceUnavailableError(RuntimeError):
    """Transport failure or a 5xx that survived the retry budget."""


class _HostThrottle:
    """Per-host minimum interval, enforced process-wide across worker threads."""

    def __init__(self, min_interval_seconds: float) -> None:
        self._min_interval = min_interval_seconds
        self._last: dict[str, float] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock_for(self, host: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(host, threading.Lock())

    def wait(self, host: str) -> None:
        # Holding the per-host lock for the sleep gives per-host concurrency 1.
        lock = self._lock_for(host)
        lock.acquire()
        try:
            elapsed = time.monotonic() - self._last.get(host, 0.0)
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
        finally:
            self._last[host] = time.monotonic()
            lock.release()


@dataclass(slots=True)
class ConditionalState:
    """ETag / Last-Modified carried between runs of the same source."""

    etag: str | None = None
    last_modified: str | None = None

    def headers(self) -> dict[str, str]:
        out: dict[str, str] = {}
        if self.etag:
            out["If-None-Match"] = self.etag
        if self.last_modified:
            out["If-Modified-Since"] = self.last_modified
        return out

    def update(self, response: httpx.Response) -> None:
        self.etag = response.headers.get("etag", self.etag)
        self.last_modified = response.headers.get("last-modified", self.last_modified)


class HttpClient:
    """Thin, polite wrapper over httpx.

    Rate limiting is two-layer: a per-source requests-per-minute budget, and a
    global per-host minimum interval. A source configured at 60 rpm against a
    host shared with another source still cannot exceed the host interval.
    """

    def __init__(
        self,
        *,
        user_agent: str,
        timeout_seconds: float = 30.0,
        max_retries: int = 3,
        per_host_min_interval_seconds: float = 2.0,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.user_agent = user_agent
        self.max_retries = max_retries
        # Injectable so backoff behaviour can be asserted without a test suite
        # that actually waits out a Retry-After.
        self._sleep = sleep
        self._throttle = _HostThrottle(per_host_min_interval_seconds)
        self._client = client or httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
        )
        self._rpm_lock = threading.Lock()
        self._rpm_window: dict[str, list[float]] = {}

    # ── budgets ──────────────────────────────────────────────────────

    def _consume_rpm(self, budget_key: str, rate_limit_rpm: int) -> None:
        if rate_limit_rpm <= 0:
            return
        while True:
            with self._rpm_lock:
                now = time.monotonic()
                window = [t for t in self._rpm_window.get(budget_key, []) if now - t < 60.0]
                if len(window) < rate_limit_rpm:
                    window.append(now)
                    self._rpm_window[budget_key] = window
                    return
                sleep_for = 60.0 - (now - window[0]) + 0.01
                self._rpm_window[budget_key] = window
            time.sleep(min(sleep_for, 60.0))

    # ── requests ─────────────────────────────────────────────────────

    def request(
        self,
        method: str,
        url: str,
        *,
        budget_key: str = "default",
        rate_limit_rpm: int = 20,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        conditional: ConditionalState | None = None,
    ) -> httpx.Response:
        host = urlsplit(url).hostname or "unknown"
        merged = dict(headers or {})
        if conditional:
            merged.update(conditional.headers())

        backoff = 1.0
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            self._consume_rpm(budget_key, rate_limit_rpm)
            self._throttle.wait(host)
            try:
                response = self._client.request(method, url, headers=merged, params=params)
            except httpx.HTTPError as exc:  # transport-level
                last_error = exc
                log.warning("http.transport_error", url=url, attempt=attempt, error=str(exc))
                self._sleep(backoff)
                backoff *= 5
                continue

            if response.status_code in (429, 503):
                retry_after = _parse_retry_after(response.headers.get("retry-after"))
                if attempt == self.max_retries:
                    raise RateLimitedError(
                        f"{response.status_code} from {host} after {attempt} attempts",
                        retry_after=retry_after,
                    )
                wait = retry_after if retry_after is not None else backoff
                log.warning("http.rate_limited", url=url, status=response.status_code, wait=wait)
                self._sleep(min(wait, 120.0))
                backoff *= 5
                continue

            if 500 <= response.status_code < 600:
                last_error = SourceUnavailableError(f"{response.status_code} from {host}")
                if attempt == self.max_retries:
                    raise last_error
                self._sleep(backoff)
                backoff *= 5
                continue

            if conditional:
                conditional.update(response)
            return response

        raise SourceUnavailableError(
            f"{url} failed after {self.max_retries} attempts: {last_error}"
        )

    def get_json(self, url: str, **kwargs: Any) -> Any:
        """GET returning parsed JSON. 304 yields None, meaning 'nothing changed'."""
        response = self.request("GET", url, **kwargs)
        if response.status_code == 304:
            return None
        response.raise_for_status()
        return response.json()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return max((when - datetime.now(UTC)).total_seconds(), 0.0)
    except (TypeError, ValueError):
        return None


class RobotsCache:
    """robots.txt, fetched, cached for 24 hours and honoured (§5.6).

    Used by the URL verification worker and any HTML fallback path. Tier 1 ATS
    JSON endpoints exist to be consumed by third parties, but the fetch is still
    gated here so that the rule lives in one place.
    """

    def __init__(self, client: HttpClient, *, ttl: timedelta = timedelta(hours=24)) -> None:
        self._client = client
        self._ttl = ttl
        self._cache: dict[str, tuple[datetime, urllib.robotparser.RobotFileParser | None]] = {}
        self._lock = threading.Lock()

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        host = parts.hostname
        if not host:
            return False
        origin = f"{parts.scheme}://{parts.netloc}"
        now = datetime.now(UTC)

        with self._lock:
            cached = self._cache.get(origin)
        if cached and now - cached[0] < self._ttl:
            parser = cached[1]
        else:
            parser = self._fetch(origin)
            with self._lock:
                self._cache[origin] = (now, parser)

        if parser is None:
            # No robots.txt, or it could not be fetched: RFC 9309 treats an
            # absent file as unrestricted, and a 5xx as a temporary full
            # disallow — we take the conservative reading only for 5xx, which
            # `_fetch` signals by returning a parser that disallows everything.
            return True
        return parser.can_fetch(self._client.user_agent, url)

    def _fetch(self, origin: str) -> urllib.robotparser.RobotFileParser | None:
        parser = urllib.robotparser.RobotFileParser()
        try:
            response = self._client.request("GET", f"{origin}/robots.txt", rate_limit_rpm=30)
        except (RateLimitedError, SourceUnavailableError, httpx.HTTPError):
            parser.parse(["User-agent: *", "Disallow: /"])
            return parser
        if response.status_code == 404:
            return None
        if response.status_code >= 500:
            parser.parse(["User-agent: *", "Disallow: /"])
            return parser
        parser.parse(response.text.splitlines())
        return parser
