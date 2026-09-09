"""Lever public postings API.

    GET https://api.lever.co/v0/postings/{company}?mode=json

The one that breaks naive integrations: Lever returns a **bare array**, not an
object, and its title field is `text`, not `title`. `payload.get("jobs", [])`
against this endpoint returns zero rows for as long as nobody looks (§5.2).
Timestamps are epoch **milliseconds**.
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

BASE_URL = "https://api.lever.co/v0/postings"

_WORKPLACE_TYPES = {
    "remote": RemoteType.REMOTE,
    "hybrid": RemoteType.HYBRID,
    "on-site": RemoteType.ONSITE,
    "onsite": RemoteType.ONSITE,
    "unspecified": None,
}


@register
class LeverAdapter(BaseSourceAdapter):
    adapter = "lever"
    platform = ATSPlatform.LEVER
    tier = 1

    def fetch(self, cursor: Cursor | None = None) -> Iterator[RawJob]:
        company = self.require("company")
        payload = self.get_json(f"{BASE_URL}/{company}", params={"mode": "json"})
        if payload is None:  # 304 Not Modified
            return
        # A bare array. Not a container. This is the whole point of the rule
        # that no adapter shares a response accessor with another.
        if not isinstance(payload, list):
            raise ValueError(
                f"{self.name}: expected a JSON array from Lever, got {type(payload).__name__}"
            )
        for job in payload:
            yield self.raw(str(job["id"]), job)

    def normalize(self, raw: RawJob) -> NormalizedJob:
        job: dict[str, Any] = raw.payload
        categories = job.get("categories") or {}
        title = job["text"]  # `text`, not `title`

        description = job.get("descriptionPlain") or html_to_text(job.get("description") or "")
        # Lever splits the body into `lists` (Requirements, Benefits, ...).
        for block in job.get("lists") or []:
            heading = (block.get("text") or "").strip()
            body = html_to_text(block.get("content") or "")
            if body:
                description = f"{description}\n\n{heading}\n{body}".strip()
        closing = job.get("additionalPlain") or html_to_text(job.get("additional") or "")
        if closing:
            description = f"{description}\n\n{closing}".strip()

        location_raw = categories.get("location") or (job.get("workplaceType") or None)
        workplace = _WORKPLACE_TYPES.get((job.get("workplaceType") or "").lower())

        return self.build(
            external_id=str(job["id"]),
            company=CompanyRef(
                name=self.config.get("company_name") or self.require("company"),
                domain=self.config.get("domain"),
                careers_url=self.config.get("careers_url"),
            ),
            title=title,
            description_text=description,
            source_url=job.get("hostedUrl") or "",
            ats_native_url=job.get("applyUrl") or job.get("hostedUrl"),
            locations=[loc for loc in [parse_location(location_raw)] if loc is not None],
            remote_type=workplace or infer_remote_type(location_raw, title, description[:1500]),
            employment_type=parse_employment_type(categories.get("commitment")),
            seniority_level=infer_seniority(title, description),
            posted_at=parse_datetime(job.get("createdAt")),  # epoch milliseconds
        )
