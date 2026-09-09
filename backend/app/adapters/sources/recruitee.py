"""Recruitee public offers API.

    GET https://{company}.recruitee.com/api/offers/

Container: `{"offers": [...]}`. Description and requirements arrive as separate
HTML fields and are concatenated. `careers_apply_url` is the authoritative
apply link when present.
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


@register
class RecruiteeAdapter(BaseSourceAdapter):
    adapter = "recruitee"
    platform = ATSPlatform.RECRUITEE
    tier = 1

    def fetch(self, cursor: Cursor | None = None) -> Iterator[RawJob]:
        company = self.require("company")
        payload = self.get_json(f"https://{company}.recruitee.com/api/offers/")
        if payload is None:
            return
        offers = payload["offers"] if isinstance(payload, dict) else []
        for offer in offers:
            yield self.raw(str(offer["id"]), offer)

    def normalize(self, raw: RawJob) -> NormalizedJob:
        offer: dict[str, Any] = raw.payload
        title = offer["title"]
        parts = [
            html_to_text(offer.get("description") or ""),
            html_to_text(offer.get("requirements") or ""),
        ]
        description = "\n\n".join(p for p in parts if p)

        location = _location(offer)
        apply_url = offer.get("careers_apply_url") or offer.get("careers_url")

        return self.build(
            external_id=str(offer["id"]),
            company=CompanyRef(
                name=self.config.get("company_name")
                or offer.get("company_name")
                or self.require("company"),
                domain=self.config.get("domain"),
                # `offer["careers_url"]` is on the shared recruitee.com host; it
                # is kept only when the operator configured a real careers page.
                careers_url=self.config.get("careers_url"),
            ),
            title=title,
            description_text=description,
            source_url=offer.get("careers_url") or apply_url or "",
            ats_native_url=apply_url,
            locations=[location] if location else [],
            remote_type=(
                RemoteType.REMOTE
                if offer.get("remote")
                else infer_remote_type(
                    location.raw if location else None, title, description[:1500]
                )
            ),
            employment_type=parse_employment_type(
                offer.get("employment_type_code") or offer.get("employment_type")
            ),
            seniority_level=infer_seniority(title, description),
            posted_at=parse_datetime(offer.get("published_at") or offer.get("created_at")),
        )


def _location(offer: dict[str, Any]) -> JobLocation | None:
    city = offer.get("city") or None
    country = offer.get("country_code") or None
    raw = offer.get("location") or ", ".join(p for p in (city, country) if p)
    if not raw:
        return None
    return JobLocation(
        raw=raw,
        city=city,
        region=offer.get("state_code") or None,
        country=country,
        is_remote=bool(offer.get("remote")),
    )
