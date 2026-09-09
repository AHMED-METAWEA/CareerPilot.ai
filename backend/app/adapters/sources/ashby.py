"""Ashby public job-board API.

    GET https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true

Container: `{"jobs": [...]}`. Apply URL: `jobUrl`. Ashby is the one Tier 1
source that publishes structured compensation, so salary is read rather than
inferred.
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
    parse_location,
)
from app.domain.models import (
    ATSPlatform,
    CompanyRef,
    Cursor,
    NormalizedJob,
    RawJob,
    RemoteType,
)

BASE_URL = "https://api.ashbyhq.com/posting-api/job-board"


@register
class AshbyAdapter(BaseSourceAdapter):
    adapter = "ashby"
    platform = ATSPlatform.ASHBY
    tier = 1

    def fetch(self, cursor: Cursor | None = None) -> Iterator[RawJob]:
        org = self.require("org")
        payload = self.get_json(f"{BASE_URL}/{org}", params={"includeCompensation": "true"})
        if payload is None:
            return
        jobs = payload["jobs"] if isinstance(payload, dict) else []
        for job in jobs:
            yield self.raw(str(job["id"]), job)

    def normalize(self, raw: RawJob) -> NormalizedJob:
        job: dict[str, Any] = raw.payload
        title = job["title"]
        description = job.get("descriptionPlain") or html_to_text(job.get("descriptionHtml") or "")

        location_strings = [job.get("location")]
        location_strings += [
            s.get("location") if isinstance(s, dict) else s
            for s in (job.get("secondaryLocations") or [])
        ]
        locations = [loc for loc in (parse_location(s) for s in location_strings) if loc]

        salary_min, salary_max, currency = _compensation(job)
        remote = RemoteType.REMOTE if job.get("isRemote") else None

        return self.build(
            external_id=str(job["id"]),
            company=CompanyRef(
                name=self.config.get("company_name") or self.require("org"),
                domain=self.config.get("domain"),
                careers_url=self.config.get("careers_url"),
            ),
            title=title,
            description_text=description,
            source_url=job.get("jobUrl") or "",
            ats_native_url=job.get("applyUrl") or job.get("jobUrl"),
            locations=locations,
            remote_type=remote or infer_remote_type(job.get("location"), title, description[:1500]),
            employment_type=parse_employment_type(job.get("employmentType")),
            seniority_level=infer_seniority(title, description),
            salary_min=salary_min,
            salary_max=salary_max,
            currency=currency,
            posted_at=parse_datetime(job.get("publishedAt") or job.get("updatedAt")),
        )


def _compensation(job: dict[str, Any]) -> tuple[float | None, float | None, str | None]:
    """Read Ashby's structured salary range, if the employer published one.

    Only a salary component is used; equity and bonus components are ignored
    rather than summed into a misleading figure.
    """
    compensation = job.get("compensation") or {}
    tiers = compensation.get("compensationTiers") or []
    for tier in tiers:
        for component in tier.get("components") or []:
            if (component.get("compensationType") or "").lower() != "salary":
                continue
            minimum = component.get("minValue")
            maximum = component.get("maxValue")
            currency = component.get("currencyCode")
            if minimum is None and maximum is None:
                continue
            return (
                float(minimum) if minimum is not None else None,
                float(maximum) if maximum is not None else None,
                currency,
            )
    return None, None, None
