"""Apply-URL liveness verification — W5 (§11.5).

    HEAD → GET → follow redirects → assert 200
         → company token present · title token present · closure phrases absent
         → set url_status ∈ {live, redirected, gone, blocked, unknown}

A posting is never shown without a `live` status inside the verification window
(§8.3). This is the single most visible quality guarantee in the product: a dead
apply link is not a stale row, it is a user who spent an hour on a role that
closed last month.

The status codes carry real distinctions:

* `gone` — 404/410, or the page says the role is closed. Suppressed and expired.
* `blocked` — 401/403, or robots.txt disallows. We cannot verify it, and that is
  not the employer's fault or the posting's; it stays unverified rather than
  being marked dead.
* `redirected` — resolves, but not to where we were sent. Usually a board that
  moved; worth re-resolving rather than trusting.

The plan schedules this for Phase 3. It is built here because the Phase 1 gates
depend on it: with no verifier, `require_verified_url` would hold back every
posting in the corpus.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.adapters.http import HttpClient, RateLimitedError, RobotsCache, SourceUnavailableError
from app.config import AppConfig
from app.domain.jobs.urls import canonicalize_url, url_host
from app.domain.models import UrlStatus

log = structlog.get_logger(__name__)

# Phrases an employer uses when a role has closed. Checked case-insensitively
# against the fetched page, and only alongside a successful fetch.
CLOSURE_PHRASES: tuple[str, ...] = (
    "no longer accepting applications",
    "this position has been filled",
    "this job is no longer available",
    "position closed",
    "job posting has expired",
    "this posting is closed",
    "applications are closed",
    "we are no longer accepting",
    "هذه الوظيفة لم تعد متاحة",
    "تم إغلاق التقديم",
)

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


@dataclass(slots=True)
class VerificationOutcome:
    posting_id: uuid.UUID
    status: UrlStatus
    http_status: int | None = None
    final_url: str | None = None
    detail: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class VerificationReport:
    checked: int = 0
    live: int = 0
    gone: int = 0
    blocked: int = 0
    redirected: int = 0
    unknown: int = 0

    def record(self, status: UrlStatus) -> None:
        self.checked += 1
        setattr(self, status.value, getattr(self, status.value) + 1)


class VerificationService:
    def __init__(self, session: Session, http: HttpClient, config: AppConfig) -> None:
        self.session = session
        self.http = http
        self.config = config
        self.robots = RobotsCache(http)

    def verify_batch(self, limit: int = 50) -> VerificationReport:
        """Verify the postings whose check is oldest (§11.5 step 1)."""
        rows = self.session.execute(
            text(
                """
                SELECT p.id, p.apply_url, p.title, c.canonical_name AS company
                  FROM job_postings p
                  LEFT JOIN companies c ON c.id = p.company_id
                 WHERE p.status = 'open'
                   AND (p.last_verified_at IS NULL
                        OR p.last_verified_at < now() - CAST(:window AS interval))
                 ORDER BY p.last_verified_at NULLS FIRST, p.posted_at DESC
                 LIMIT :limit
                """
            ),
            {"window": f"{self.config.verification.reverify_after_hours} hours", "limit": limit},
        ).all()

        report = VerificationReport()
        for row in rows:
            outcome = self.verify_one(
                posting_id=row.id,
                apply_url=row.apply_url,
                title=row.title,
                company=row.company,
            )
            self._persist(outcome)
            report.record(outcome.status)

        log.info(
            "verify_urls.completed",
            checked=report.checked,
            live=report.live,
            gone=report.gone,
            blocked=report.blocked,
        )
        return report

    def verify_one(
        self, *, posting_id: uuid.UUID, apply_url: str, title: str, company: str | None
    ) -> VerificationOutcome:
        """Fetch one apply URL and decide what its status is."""
        if not apply_url:
            return VerificationOutcome(posting_id, UrlStatus.UNKNOWN, detail={"reason": "no url"})

        if not self.robots.allowed(apply_url):
            # Not permission to crawl, so not evidence of anything about the job.
            return VerificationOutcome(
                posting_id, UrlStatus.BLOCKED, detail={"reason": "robots.txt disallows"}
            )

        try:
            response = self.http.request(
                "GET",
                apply_url,
                budget_key=f"verify:{url_host(apply_url)}",
                rate_limit_rpm=20,
            )
        except RateLimitedError:
            # The host is throttling us, which says nothing about the posting.
            return VerificationOutcome(
                posting_id, UrlStatus.UNKNOWN, detail={"reason": "rate limited"}
            )
        except (SourceUnavailableError, httpx.HTTPError) as exc:
            return VerificationOutcome(
                posting_id, UrlStatus.UNKNOWN, detail={"reason": f"unreachable: {exc}"[:200]}
            )

        return self._assess(posting_id, response, apply_url, title, company)

    def _assess(
        self,
        posting_id: uuid.UUID,
        response: httpx.Response,
        apply_url: str,
        title: str,
        company: str | None,
    ) -> VerificationOutcome:
        status_code = response.status_code
        final_url = str(response.url)

        if status_code in (404, 410):
            return VerificationOutcome(
                posting_id, UrlStatus.GONE, status_code, final_url, {"reason": "not found"}
            )
        if status_code in (401, 403):
            return VerificationOutcome(
                posting_id, UrlStatus.BLOCKED, status_code, final_url, {"reason": "access denied"}
            )
        if status_code >= 400:
            return VerificationOutcome(
                posting_id, UrlStatus.UNKNOWN, status_code, final_url, {"reason": "http error"}
            )

        page = _visible_text(response.text)

        closure = next((phrase for phrase in CLOSURE_PHRASES if phrase in page), None)
        if closure:
            return VerificationOutcome(
                posting_id, UrlStatus.GONE, status_code, final_url, {"closure_phrase": closure}
            )

        title_ok = _token_present(title, page)
        company_ok = company is None or _token_present(company, page)

        if not (title_ok or company_ok):
            # 200, but neither the role nor the employer appears: almost always a
            # board index or a generic careers page after a silent redirect.
            return VerificationOutcome(
                posting_id,
                UrlStatus.REDIRECTED,
                status_code,
                final_url,
                {"reason": "neither title nor company found on page"},
            )

        redirected = canonicalize_url(final_url) != canonicalize_url(apply_url)
        return VerificationOutcome(
            posting_id,
            UrlStatus.REDIRECTED if redirected else UrlStatus.LIVE,
            status_code,
            final_url,
            {"title_found": title_ok, "company_found": company_ok},
        )

    def _persist(self, outcome: VerificationOutcome) -> None:
        import json

        self.session.execute(
            text(
                """
                UPDATE job_postings
                   SET url_status = :status,
                       last_verified_at = now(),
                       verification_detail = CAST(:detail AS jsonb),
                       status = CASE WHEN :status = 'gone' THEN 'expired' ELSE status END,
                       updated_at = now()
                 WHERE id = :id
                """
            ),
            {
                "id": outcome.posting_id,
                "status": outcome.status.value,
                "detail": json.dumps(
                    {
                        **outcome.detail,
                        "http_status": outcome.http_status,
                        "final_url": outcome.final_url,
                        "checked_at": datetime.now(UTC).isoformat(),
                    },
                    default=str,
                ),
            },
        )


def _visible_text(html: str) -> str:
    """Rough text of a page, lowercased. Good enough for token presence."""
    without_scripts = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.I | re.S)
    return _WS.sub(" ", _TAG.sub(" ", without_scripts)).casefold()


def _token_present(value: str, page: str) -> bool:
    """Is the distinctive part of `value` on the page?

    Whole-string matching fails constantly: employers reformat titles between
    their feed and their page ("Sr." vs "Senior"). Matching the longest tokens
    is tolerant of that without being tolerant of a wrong page.
    """
    tokens = re.findall(r"[\w\u0600-\u06FF]{4,}", value.casefold())
    if not tokens:
        return True
    distinctive = sorted(tokens, key=len, reverse=True)[:3]
    return any(token in page for token in distinctive)
