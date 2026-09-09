"""Workable careers-widget API.

    GET https://apply.workable.com/api/v1/widget/accounts/{sub}?details=true

Container: `{"jobs": [...]}`.

**This is the widget endpoint their embeddable careers page calls, not their
documented authenticated API.** It is public and functional, but must be
treated as capable of changing without notice (§5.2, R3). Two consequences,
both implemented here: the payload shape is validated rather than assumed, and
a shape failure raises loudly so the source-health alert fires instead of the
run quietly recording zero rows.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

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
)

BASE_URL = "https://apply.workable.com/api/v1/widget/accounts"


@register
class WorkableAdapter(BaseSourceAdapter):
    adapter = "workable"
    platform = ATSPlatform.WORKABLE
    tier = 1

    def fetch(self, cursor: Cursor | None = None) -> Iterator[RawJob]:
        subdomain = self.require("subdomain")
        payload = self.get_json(f"{BASE_URL}/{subdomain}", params={"details": "true"})
        if payload is None:
            return
        if not isinstance(payload, dict) or "jobs" not in payload:
            # Untrusted endpoint: fail loudly rather than degrade to silence.
            raise ValueError(
                f"{self.name}: unexpected Workable widget payload "
                f"({type(payload).__name__}, keys={sorted(payload)[:8] if isinstance(payload, dict) else '-'})"
            )
        account_name = payload.get("name") or payload.get("description")
        for job in payload["jobs"]:
            job = dict(job)
            job.setdefault("_account_name", account_name)
            yield self.raw(str(job.get("shortcode") or job.get("id")), job)

    def normalize(self, raw: RawJob) -> NormalizedJob:
        job: dict[str, Any] = raw.payload
        title = job["title"]

        body_parts = [
            html_to_text(job.get("description") or ""),
            html_to_text(job.get("requirements") or ""),
            html_to_text(job.get("benefits") or ""),
        ]
        description = "\n\n".join(part for part in body_parts if part)

        location = _location(job)
        telecommuting = bool(job.get("telecommuting"))

        return self.build(
            external_id=str(job.get("shortcode") or job["id"]),
            company=CompanyRef(
                name=self.config.get("company_name")
                or job.get("_account_name")
                or self.require("subdomain"),
                domain=self.config.get("domain"),
                careers_url=self.config.get("careers_url"),
            ),
            title=title,
            description_text=description,
            source_url=job.get("url") or job.get("application_url") or "",
            ats_native_url=job.get("application_url") or job.get("url"),
            locations=[location] if location else [],
            remote_type=(
                RemoteType.REMOTE
                if telecommuting
                else infer_remote_type(
                    location.raw if location else None, title, description[:1500]
                )
            ),
            employment_type=parse_employment_type(job.get("employment_type")),
            seniority_level=infer_seniority(title, description),
            posted_at=parse_datetime(job.get("published_on") or job.get("created_at")),
        )


def _location(job: dict[str, Any]) -> JobLocation | None:
    """Workable sends a structured location object; use it rather than re-parsing."""
    location = job.get("location")
    if not isinstance(location, dict):
        return None
    city = location.get("city") or None
    region = location.get("region") or None
    country_code = location.get("country_code") or location.get("countryCode")
    country_name = location.get("country")
    parts = [p for p in (city, region, country_name) if p]
    raw = ", ".join(parts) or (country_code or "")
    if not raw:
        return None
    return JobLocation(
        raw=raw,
        city=city,
        region=region,
        country=country_code,
        is_remote=bool(job.get("telecommuting") or location.get("telecommuting")),
    )
