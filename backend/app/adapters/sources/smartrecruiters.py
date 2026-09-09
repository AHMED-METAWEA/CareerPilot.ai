"""SmartRecruiters public postings API.

    GET https://api.smartrecruiters.com/v1/companies/{id}/postings?limit=100&offset=N

Container: `{"content": [...]}`. Title field: `name`.

The list response carries no description, so a posting's body needs a second
request. That second request is **not** made during the run: a board like
BoschGroup lists 4,800 postings, and fetching a detail per posting would turn
one hourly run into 4,800 requests against one host — impolite, slow, and
guaranteed to hit a rate limit.

Instead `fetch()` yields list rows only, and the ingestion service queues
`fetch_details` for the postings that still lack a body. Detail fetching is
therefore rate-limited, resumable, and bounded per run, and a posting is useful
(title, location, apply URL) from the first run regardless.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import structlog

from app.adapters.http import RateLimitedError, SourceUnavailableError
from app.adapters.sources.base import BaseSourceAdapter, register
from app.domain.jobs.normalize import (
    html_to_text,
    infer_remote_type,
    infer_seniority,
    parse_datetime,
    parse_employment_type,
)
from app.domain.models import (
    ATSPlatform,
    CompanyRef,
    Cursor,
    JobLocation,
    NormalizedJob,
    RawJob,
    RemoteType,
    SourceHealth,
)

BASE_URL = "https://api.smartrecruiters.com/v1/companies"
PAGE_SIZE = 100
MAX_PAGES = 20

log = structlog.get_logger(__name__)


@register
class SmartRecruitersAdapter(BaseSourceAdapter):
    adapter = "smartrecruiters"
    platform = ATSPlatform.SMARTRECRUITERS
    tier = 1

    supports_details = True

    def fetch(self, cursor: Cursor | None = None) -> Iterator[RawJob]:
        company_id = self.require("company_id")
        offset = 0

        for _ in range(MAX_PAGES):
            payload = self.get_json(
                f"{BASE_URL}/{company_id}/postings",
                params={"limit": PAGE_SIZE, "offset": offset},
            )
            if payload is None:
                return
            postings = payload["content"] if isinstance(payload, dict) else []
            if not postings:
                return

            for posting in postings:
                yield self.raw(str(posting["id"]), dict(posting))

            offset += len(postings)
            total = payload.get("totalFound") if isinstance(payload, dict) else None
            if total is not None and offset >= int(total):
                return

    def health(self) -> SourceHealth:
        """Probe the list endpoint only.

        The inherited health check drains `fetch()`, which for this adapter also
        fetches one detail document per posting — hundreds of requests to answer
        "is this board alive?". Health must never be more expensive than the run
        it is checking.
        """
        checked_at = datetime.now(UTC)
        try:
            payload = self.get_json(
                f"{BASE_URL}/{self.require('company_id')}/postings",
                params={"limit": 1, "offset": 0},
            )
        except Exception as exc:
            return SourceHealth(
                source_name=self.name,
                reachable=False,
                checked_at=checked_at,
                detail=f"{type(exc).__name__}: {exc}",
            )
        total = int(payload.get("totalFound", 0)) if isinstance(payload, dict) else 0
        return SourceHealth(
            source_name=self.name, reachable=True, checked_at=checked_at, detail=f"{total} postings"
        )

    def fetch_detail(self, external_id: str) -> dict[str, Any] | None:
        """Fetch one posting's detail document. A failure degrades one row, not the run."""
        company_id = self.require("company_id")
        try:
            detail = self.get_json(f"{BASE_URL}/{company_id}/postings/{external_id}")
        except (RateLimitedError, SourceUnavailableError) as exc:
            log.warning(
                "smartrecruiters.detail_failed",
                source=self.name,
                posting_id=external_id,
                error=str(exc),
            )
            return None
        return detail if isinstance(detail, dict) else None

    def describe(self, detail: dict[str, Any]) -> str:
        """Body text from a detail document, for the `fetch_details` task."""
        return _description(detail)

    def normalize(self, raw: RawJob) -> NormalizedJob:
        posting: dict[str, Any] = raw.payload
        detail: dict[str, Any] = posting.get("_detail") or {}
        title = posting.get("name") or detail.get("name") or ""  # `name`, not `title`

        description = _description(detail)
        location = _location(posting.get("location") or detail.get("location") or {})
        company_identifier = (
            (posting.get("company") or {}).get("identifier") or self.config.get("company_id") or ""
        )
        # SmartRecruiters' `ref` is the API URL, not a page a human can open.
        # The public board URL follows a fixed, documented pattern.
        public_url = (
            detail.get("applyUrl")
            or detail.get("postingUrl")
            or f"https://jobs.smartrecruiters.com/{company_identifier}/{posting['id']}"
        )

        return self.build(
            external_id=str(posting["id"]),
            company=CompanyRef(
                name=self.config.get("company_name")
                or (posting.get("company") or {}).get("name")
                or self.require("company_id"),
                domain=self.config.get("domain"),
                careers_url=self.config.get("careers_url"),
            ),
            title=title,
            description_text=description,
            source_url=public_url,
            ats_native_url=public_url,
            locations=[location] if location else [],
            remote_type=(
                RemoteType.REMOTE
                if (posting.get("location") or {}).get("remote")
                else infer_remote_type(
                    location.raw if location else None, title, description[:1500]
                )
            ),
            employment_type=parse_employment_type(
                (posting.get("typeOfEmployment") or {}).get("label")
            ),
            seniority_level=infer_seniority(title, description),
            posted_at=parse_datetime(posting.get("releasedDate") or posting.get("createdOn")),
        )


def _description(detail: dict[str, Any]) -> str:
    """Flatten SmartRecruiters' sectioned job ad into one text body."""
    job_ad = detail.get("jobAd") or {}
    sections = (job_ad.get("sections") or {}) if isinstance(job_ad, dict) else {}
    ordered = ("companyDescription", "jobDescription", "qualifications", "additionalInformation")
    parts: list[str] = []
    for key in ordered:
        section = sections.get(key) or {}
        text = html_to_text(section.get("text") or "")
        if text:
            title = section.get("title")
            parts.append(f"{title}\n{text}" if title else text)
    return "\n\n".join(parts)


def _location(location: dict[str, Any]) -> JobLocation | None:
    if not location:
        return None
    city = location.get("city") or None
    region = location.get("region") or None
    country = location.get("country") or None
    raw = ", ".join(p for p in (city, region, country) if p)
    if not raw:
        return None
    return JobLocation(
        raw=raw,
        city=city,
        region=region,
        country=country if country and len(country) == 2 else None,
        is_remote=bool(location.get("remote")),
    )
