"""Domain value objects shared across the pipeline.

Everything here is pure data. Nothing in this module performs I/O, and nothing
imports an adapter, a service or the database layer (§4.4, §14.1).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ATSPlatform(StrEnum):
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    WORKABLE = "workable"
    SMARTRECRUITERS = "smartrecruiters"
    RECRUITEE = "recruitee"
    WORKDAY = "workday"
    ICIMS = "icims"
    TALEO = "taleo"
    BREEZY = "breezy"
    JOBVITE = "jobvite"
    TEAMTAILOR = "teamtailor"
    BAMBOOHR = "bamboohr"
    PERSONIO = "personio"
    ORACLE_HCM = "oracle_hcm"
    SUCCESSFACTORS = "successfactors"
    UNKNOWN = "unknown"
    """'unknown' is a valid, honest value — never a guess (Appendix C, row 5)."""


class DetectionMethod(StrEnum):
    CONSTRUCTION = "construction"
    URL = "url"
    FINGERPRINT = "fingerprint"
    REDIRECT = "redirect"
    NONE = "none"


class RemoteType(StrEnum):
    ONSITE = "onsite"
    HYBRID = "hybrid"
    REMOTE = "remote"


class EmploymentType(StrEnum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    INTERNSHIP = "internship"
    TEMPORARY = "temporary"
    OTHER = "other"


class Seniority(StrEnum):
    INTERN = "intern"
    JUNIOR = "junior"
    MID = "mid"
    SENIOR = "senior"
    STAFF = "staff"
    PRINCIPAL = "principal"


SENIORITY_ORDER: tuple[Seniority, ...] = (
    Seniority.INTERN,
    Seniority.JUNIOR,
    Seniority.MID,
    Seniority.SENIOR,
    Seniority.STAFF,
    Seniority.PRINCIPAL,
)


class UrlStatus(StrEnum):
    UNKNOWN = "unknown"
    LIVE = "live"
    REDIRECTED = "redirected"
    GONE = "gone"
    BLOCKED = "blocked"


class PostingStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    EXPIRED = "expired"


class ApplyUrlMethod(StrEnum):
    """Apply-URL resolution precedence (Appendix D), most authoritative first."""

    ATS_NATIVE = "ats_native"
    CANONICAL_LINK = "canonical_link"
    JSON_LD = "json_ld"
    REDIRECT_TERMINUS = "redirect_terminus"
    SOURCE_URL = "source_url"


class JobLocation(BaseModel):
    model_config = ConfigDict(frozen=True)

    raw: str
    city: str | None = None
    region: str | None = None
    country: str | None = Field(default=None, description="ISO 3166-1 alpha-2, uppercase")
    is_remote: bool = False

    @field_validator("country")
    @classmethod
    def _upper_iso2(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().upper()
        return v if len(v) == 2 else None


class CompanyRef(BaseModel):
    """What an adapter can say about an employer before entity resolution runs."""

    model_config = ConfigDict(frozen=True)

    name: str
    domain: str | None = None
    careers_url: str | None = None


class RawJob(BaseModel):
    """A payload exactly as the source returned it. Persisted verbatim (§11.2 step 3)."""

    model_config = ConfigDict(frozen=True)

    source_name: str
    external_id: str
    payload: dict[str, Any]
    fetched_at: datetime


class NormalizedJob(BaseModel):
    """The single canonical posting shape every adapter maps onto (§3.1).

    Strict: an unexpected field is an integration bug, not something to absorb
    silently — a silent shape change is exactly how a source dies unnoticed (R2).
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source_name: str
    external_id: str

    company: CompanyRef
    title: str
    title_normalized: str
    description_text: str

    locations: list[JobLocation] = Field(default_factory=list)
    remote_type: RemoteType | None = None
    employment_type: EmploymentType | None = None
    seniority_level: Seniority | None = None

    salary_min: float | None = None
    salary_max: float | None = None
    currency: str | None = Field(default=None, max_length=3)

    posted_at: datetime | None = None
    expires_at: datetime | None = None

    ats_platform: ATSPlatform = ATSPlatform.UNKNOWN
    ats_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    detection_method: DetectionMethod = DetectionMethod.NONE

    apply_url: str
    source_url: str
    canonical_url: str | None = None
    apply_url_method: ApplyUrlMethod = ApplyUrlMethod.ATS_NATIVE

    language: str | None = Field(default=None, max_length=2)
    content_simhash: int | None = None
    status: PostingStatus = PostingStatus.OPEN

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, v: str | None) -> str | None:
        return v.upper() if v else None


class SourceHealth(BaseModel):
    """Reported per source; drives the §17.1 source-health signal."""

    model_config = ConfigDict(frozen=True)

    source_name: str
    reachable: bool
    checked_at: datetime
    detail: str | None = None


class Cursor(BaseModel):
    """Opaque per-adapter pagination state, round-tripped through source_runs."""

    model_config = ConfigDict(extra="allow")

    value: str | None = None
    page: int | None = None
    updated_after: datetime | None = None


class ATSDetection(BaseModel):
    model_config = ConfigDict(frozen=True)

    platform: ATSPlatform
    confidence: float = Field(ge=0.0, le=1.0)
    method: DetectionMethod


class ResolvedApplyUrl(BaseModel):
    model_config = ConfigDict(frozen=True)

    url: str
    method: ApplyUrlMethod


CompanyMatchMethod = Literal["domain", "alias", "fuzzy", "new"]


class CompanyResolution(BaseModel):
    """Outcome of company entity resolution (§11.3).

    `needs_review` is set whenever confidence sits in the band where a merge is
    plausible but unproven — those go to a human queue rather than being guessed.
    """

    model_config = ConfigDict(frozen=True)

    company_key: str | None
    confidence: float = Field(ge=0.0, le=1.0)
    method: CompanyMatchMethod
    needs_review: bool = False
    matched_alias: str | None = None


# ── Matching projections (§8) ─────────────────────────────────────────
#
# What the gates and the scorer actually need from a profile and a posting.
# Deliberately narrow: the domain cannot reach for a database row, so anything
# missing here is a decision about what matching is allowed to consider.


class WorkAuthorization(BaseModel):
    """What the candidate may do in one country, and on what basis."""

    model_config = ConfigDict(frozen=True)

    country: str = Field(description="ISO 3166-1 alpha-2, uppercase")
    status: Literal["citizen", "permanent_resident", "work_visa", "requires_sponsorship", "none"]

    @property
    def is_authorized(self) -> bool:
        return self.status in {"citizen", "permanent_resident", "work_visa"}


class LanguageAbility(BaseModel):
    model_config = ConfigDict(frozen=True)

    language: str
    cefr: Literal["A1", "A2", "B1", "B2", "C1", "C2", "native"] | None = None

    @property
    def level(self) -> int:
        order = {"A1": 1, "A2": 2, "B1": 3, "B2": 4, "C1": 5, "C2": 6, "native": 7}
        return order.get(self.cefr or "", 0)


class CandidateSnapshot(BaseModel):
    """The profile as the matcher sees it."""

    model_config = ConfigDict(frozen=True)

    profile_id: str
    years_experience: float | None = None
    seniority_level: Seniority | None = None
    locations: tuple[str, ...] = ()
    """Declared commutable set, free text as the candidate wrote it."""
    countries: tuple[str, ...] = ()
    """ISO codes derived from `locations`, for the location gate."""
    open_to_remote: bool = True
    work_authorization: tuple[WorkAuthorization, ...] = ()
    languages: tuple[LanguageAbility, ...] = ()
    skills: frozenset[str] = frozenset()
    """Canonical skill names. Resolution happened during profile construction."""
    bullets: tuple[str, ...] = ()
    """CV bullets, for requirement-level alignment."""


class PostingRequirement(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    kind: Literal["skill", "experience", "education", "auth", "language"] = "skill"
    is_must_have: bool = False
    skill: str | None = None
    """Canonical skill name, when the requirement resolved to one."""
    alternatives: tuple[str, ...] = ()
    """Other skills that satisfy this same requirement.

    "Python or Java or Go" is one thing to know, not three. Counting the
    alternatives separately means a candidate who meets the requirement scores a
    third of it."""

    def satisfied_by(self, held: frozenset[str]) -> bool:
        if self.skill and self.skill in held:
            return True
        return any(alternative in held for alternative in self.alternatives)


class PostingSnapshot(BaseModel):
    """The posting as the matcher sees it."""

    model_config = ConfigDict(frozen=True)

    posting_id: str
    title: str
    seniority_level: Seniority | None = None
    remote_type: RemoteType | None = None
    countries: tuple[str, ...] = ()
    cities: tuple[str, ...] = ()
    posted_at: datetime | None = None
    url_status: UrlStatus = UrlStatus.UNKNOWN
    last_verified_at: datetime | None = None
    min_years: float | None = None
    """A hard minimum stated by the posting, not inferred from its title."""
    requires_authorization_in: tuple[str, ...] = ()
    offers_sponsorship: bool | None = None
    required_languages: tuple[LanguageAbility, ...] = ()
    requirements: tuple[PostingRequirement, ...] = ()
