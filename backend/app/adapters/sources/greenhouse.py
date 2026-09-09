"""Greenhouse public job-board API.

    GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true

Container: `{"jobs": [...]}`. Title field: `title`. Apply URL: `absolute_url`,
which is authoritative and stored verbatim.
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
    parse_location,
)
from app.domain.models import ATSPlatform, CompanyRef, Cursor, NormalizedJob, RawJob

BASE_URL = "https://boards-api.greenhouse.io/v1/boards"


@register
class GreenhouseAdapter(BaseSourceAdapter):
    adapter = "greenhouse"
    platform = ATSPlatform.GREENHOUSE
    tier = 1

    def fetch(self, cursor: Cursor | None = None) -> Iterator[RawJob]:
        token = self.require("board_token")
        payload = self.get_json(
            f"{BASE_URL}/{token}/jobs",
            params={"content": "true", "pay_transparency": "true"},
        )
        if payload is None:  # 304 Not Modified
            return
        # Greenhouse wraps results in an object. This unpacking is deliberately
        # local to this adapter (Appendix B integration rule).
        jobs = payload["jobs"] if isinstance(payload, dict) else []
        for job in jobs:
            yield self.raw(str(job["id"]), job)

    def normalize(self, raw: RawJob) -> NormalizedJob:
        job: dict[str, Any] = raw.payload
        description = html_to_text(job.get("content") or "")
        location_raw = (job.get("location") or {}).get("name")
        offices = [
            o.get("name")
            for o in (job.get("offices") or [])
            if isinstance(o, dict) and o.get("name")
        ]
        locations = [
            loc
            for loc in (parse_location(location_raw), *(parse_location(o) for o in offices))
            if loc is not None
        ]
        # De-duplicate location strings while preserving order: a posting often
        # repeats its office name in `location.name`.
        seen: set[str] = set()
        unique_locations = []
        for location in locations:
            if location.raw not in seen:
                seen.add(location.raw)
                unique_locations.append(location)
        locations = unique_locations

        return self.build(
            external_id=str(job["id"]),
            company=CompanyRef(
                name=self.config.get("company_name") or self.require("board_token"),
                domain=self.config.get("domain"),
                # Deliberately not `job["absolute_url"]`: that is a job URL on a
                # shared ATS host, and using it as the employer's careers URL
                # collapses every Greenhouse employer into one company.
                careers_url=self.config.get("careers_url"),
            ),
            title=job["title"],
            description_text=description,
            source_url=job.get("absolute_url") or "",
            ats_native_url=job.get("absolute_url"),
            locations=locations,
            remote_type=infer_remote_type(location_raw, job.get("title"), description[:1500]),
            seniority_level=infer_seniority(job.get("title", ""), description),
            posted_at=parse_datetime(job.get("first_published") or job.get("updated_at")),
        )
