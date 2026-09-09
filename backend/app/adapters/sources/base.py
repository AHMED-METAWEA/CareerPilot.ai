"""Shared plumbing for source adapters — and deliberately nothing more.

What is shared here: HTTP conduct, the derived fields every posting needs
(normalised title, language, SimHash), and the registry.

What is *not* shared, ever: reading the response container. Greenhouse, Ashby,
Workable and SmartRecruiters wrap their results in an object; Lever returns a
bare array. A shared `data.get("jobs", [])` would silently yield zero rows for
Lever, and the failure would be invisible without per-source health checks
(§5.2, Appendix B). Every adapter therefore unpacks its own payload.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, ClassVar

import structlog

from app.adapters.http import HttpClient, RateLimitedError, SourceUnavailableError
from app.domain.dedup.simhash import simhash64, to_signed
from app.domain.jobs.normalize import detect_language, normalize_title
from app.domain.jobs.urls import canonicalize_url, resolve_apply_url
from app.domain.models import (
    ATSPlatform,
    CompanyRef,
    Cursor,
    DetectionMethod,
    EmploymentType,
    JobLocation,
    NormalizedJob,
    RawJob,
    RemoteType,
    Seniority,
    SourceHealth,
)

log = structlog.get_logger(__name__)


class AdapterConfigError(ValueError):
    """The source registry row is missing something the adapter requires."""


class BaseSourceAdapter(ABC):
    adapter: ClassVar[str]
    platform: ClassVar[ATSPlatform] = ATSPlatform.UNKNOWN
    tier: ClassVar[int] = 1
    """Tier 1 = employer's own ATS (authoritative). Tier 2 = aggregator."""
    supports_details: ClassVar[bool] = False
    """True when a posting body needs a second request per posting.

    Such adapters yield list rows from `fetch()` and implement `fetch_detail`
    plus `describe`; the ingestion service queues the bodies separately so one
    run never becomes thousands of requests against one host."""

    def __init__(
        self,
        *,
        name: str,
        config: dict[str, Any],
        http: HttpClient,
        rate_limit_rpm: int = 20,
    ) -> None:
        self.name = name
        self.config = config
        self.http = http
        self.rate_limit_rpm = rate_limit_rpm

    # ── protocol surface ─────────────────────────────────────────────

    @abstractmethod
    def fetch(self, cursor: Cursor | None = None) -> Iterator[RawJob]:
        """Yield raw payloads, one per posting, exactly as the source returned them."""

    @abstractmethod
    def normalize(self, raw: RawJob) -> NormalizedJob:
        """Map this source's field vocabulary onto the canonical shape."""

    def health(self) -> SourceHealth:
        """Reachability probe. Counts rows so a silent empty feed is visible (R2)."""
        checked_at = datetime.now(UTC)
        try:
            count = sum(1 for _ in self.fetch())
        except (RateLimitedError, SourceUnavailableError) as exc:
            return SourceHealth(
                source_name=self.name, reachable=False, checked_at=checked_at, detail=str(exc)
            )
        except Exception as exc:  # adapter/schema failure is a health failure too
            return SourceHealth(
                source_name=self.name,
                reachable=False,
                checked_at=checked_at,
                detail=f"{type(exc).__name__}: {exc}",
            )
        return SourceHealth(
            source_name=self.name,
            reachable=True,
            checked_at=checked_at,
            detail=f"{count} postings",
        )

    # ── helpers ──────────────────────────────────────────────────────

    def require(self, key: str) -> str:
        value = self.config.get(key)
        if not value or not isinstance(value, str):
            raise AdapterConfigError(f"{self.name}: config.{key} is required")
        return value

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        return self.http.get_json(
            url, params=params, budget_key=self.name, rate_limit_rpm=self.rate_limit_rpm
        )

    def raw(self, external_id: str, payload: dict[str, Any]) -> RawJob:
        return RawJob(
            source_name=self.name,
            external_id=str(external_id),
            payload=payload,
            fetched_at=datetime.now(UTC),
        )

    def build(
        self,
        *,
        external_id: str,
        company: CompanyRef,
        title: str,
        description_text: str,
        source_url: str,
        ats_native_url: str | None = None,
        locations: list[JobLocation] | None = None,
        remote_type: RemoteType | None = None,
        employment_type: EmploymentType | None = None,
        seniority_level: Seniority | None = None,
        salary_min: float | None = None,
        salary_max: float | None = None,
        currency: str | None = None,
        posted_at: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> NormalizedJob:
        """Assemble a NormalizedJob, deriving everything that is derivable.

        Derived here rather than per adapter: the normalised title, the content
        SimHash, the language, the canonical URL, and the apply-URL choice —
        each of which must behave identically across sources or dedup and the
        gates quietly disagree with themselves.
        """
        apply = resolve_apply_url(ats_native=ats_native_url, source_url=source_url)
        detection = DetectionMethod.CONSTRUCTION if self.tier == 1 else DetectionMethod.URL
        confidence = 1.00 if self.tier == 1 else 0.95
        return NormalizedJob(
            source_name=self.name,
            external_id=str(external_id),
            company=company,
            title=title.strip(),
            title_normalized=normalize_title(title),
            description_text=description_text,
            locations=locations or [],
            remote_type=remote_type,
            employment_type=employment_type,
            seniority_level=seniority_level,
            salary_min=salary_min,
            salary_max=salary_max,
            currency=currency,
            posted_at=posted_at,
            expires_at=expires_at,
            ats_platform=self.platform,
            ats_confidence=confidence,
            detection_method=detection,
            apply_url=apply.url,
            apply_url_method=apply.method,
            source_url=source_url,
            canonical_url=canonicalize_url(apply.url),
            language=detect_language(f"{title}\n{description_text}"),
            content_simhash=to_signed(simhash64(description_text)),
        )


ADAPTERS: dict[str, type[BaseSourceAdapter]] = {}


def register(cls: type[BaseSourceAdapter]) -> type[BaseSourceAdapter]:
    """Register an adapter under its `adapter` key. One file plus one DB row (§4.4)."""
    if not getattr(cls, "adapter", None):
        raise AdapterConfigError(f"{cls.__name__} must define `adapter`")
    ADAPTERS[cls.adapter] = cls
    return cls


def build_adapter(
    adapter: str,
    *,
    name: str,
    config: dict[str, Any],
    http: HttpClient,
    rate_limit_rpm: int = 20,
) -> BaseSourceAdapter:
    try:
        cls = ADAPTERS[adapter]
    except KeyError:
        raise AdapterConfigError(
            f"unknown adapter '{adapter}'; registered: {sorted(ADAPTERS)}"
        ) from None
    return cls(name=name, config=config, http=http, rate_limit_rpm=rate_limit_rpm)
