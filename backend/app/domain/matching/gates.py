"""Eligibility gates (§8.3).

Boolean rules, evaluated before any expensive computation. A failed gate
excludes the posting and records *why*, so `/matches/withheld` can tell a user
that a role was hidden because it needs authorisation they do not hold — rather
than leaving them to wonder where it went.

Two principles decide the edge cases:

* **Absence of evidence is not a failure.** A posting that says nothing about
  work authorisation does not fail the authorisation gate. Gating on silence
  would hide most of the corpus, since most postings are silent about most
  things.
* **The gate is the honest place for hard constraints.** Anything a candidate
  simply cannot do — no visa, wrong continent for an onsite role — belongs
  here, not as a small deduction inside a score that could be outweighed by a
  good skill match.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from app.domain.models import (
    CandidateSnapshot,
    PostingSnapshot,
    RemoteType,
    UrlStatus,
)


class GateFailure(StrEnum):
    WORK_AUTHORIZATION = "work_authorization"
    LOCATION = "location"
    SENIORITY_FLOOR = "seniority_floor"
    LANGUAGE = "language"
    FRESHNESS = "freshness"
    UNVERIFIED_URL = "unverified_url"


HUMAN_READABLE: dict[GateFailure, str] = {
    GateFailure.WORK_AUTHORIZATION: (
        "This role needs work authorisation you have not declared, and the employer "
        "does not mention sponsorship."
    ),
    GateFailure.LOCATION: "This role is on-site or hybrid outside the locations you gave.",
    GateFailure.SENIORITY_FLOOR: "This role states a minimum experience level well above yours.",
    GateFailure.LANGUAGE: "This role requires a language level you have not declared.",
    GateFailure.FRESHNESS: "This posting is older than the freshness window.",
    GateFailure.UNVERIFIED_URL: "We could not confirm this posting is still live.",
}


@dataclass(frozen=True, slots=True)
class GateConfig:
    max_years_shortfall: float = 2.0
    max_posting_age_days: int = 45
    require_verified_url: bool = True
    verification_window_hours: int = 48


@dataclass(frozen=True, slots=True)
class GateResult:
    passed: bool
    failures: tuple[GateFailure, ...]

    def reasons(self) -> tuple[str, ...]:
        return tuple(HUMAN_READABLE[failure] for failure in self.failures)


def evaluate_gates(
    candidate: CandidateSnapshot,
    posting: PostingSnapshot,
    config: GateConfig,
    *,
    now: datetime | None = None,
) -> GateResult:
    """Run every gate and return all failures, not just the first.

    All of them, because a user asking why a role was withheld deserves the
    whole answer, and because a single reported reason makes the gate set look
    arbitrary when it is not.
    """
    now = now or datetime.now(UTC)
    failures: list[GateFailure] = []

    if not _work_authorization_ok(candidate, posting):
        failures.append(GateFailure.WORK_AUTHORIZATION)
    if not _location_ok(candidate, posting):
        failures.append(GateFailure.LOCATION)
    if not _seniority_ok(candidate, posting, config):
        failures.append(GateFailure.SENIORITY_FLOOR)
    if not _language_ok(candidate, posting):
        failures.append(GateFailure.LANGUAGE)

    freshness, url = _freshness_ok(posting, config, now)
    if not freshness:
        failures.append(GateFailure.FRESHNESS)
    if not url:
        failures.append(GateFailure.UNVERIFIED_URL)

    return GateResult(passed=not failures, failures=tuple(failures))


def _work_authorization_ok(candidate: CandidateSnapshot, posting: PostingSnapshot) -> bool:
    """Fails only when the posting requires authorisation the candidate lacks
    *and* offers no sponsorship. Silence on either side passes."""
    if not posting.requires_authorization_in:
        return True
    if posting.offers_sponsorship:
        return True

    authorized = {
        auth.country.upper() for auth in candidate.work_authorization if auth.is_authorized
    }
    return any(country.upper() in authorized for country in posting.requires_authorization_in)


def _location_ok(candidate: CandidateSnapshot, posting: PostingSnapshot) -> bool:
    """Remote roles always pass. On-site and hybrid must be somewhere reachable."""
    if posting.remote_type is RemoteType.REMOTE:
        return candidate.open_to_remote
    if posting.remote_type is None and not posting.countries and not posting.cities:
        return True  # nothing stated: not a reason to hide the role
    if not candidate.countries and not candidate.locations:
        return True  # candidate declared nothing: gate cannot fire honestly

    candidate_countries = {country.upper() for country in candidate.countries}
    posting_countries = {country.upper() for country in posting.countries if country}
    if posting_countries and candidate_countries:
        # A hybrid role in a country the candidate cannot reach is not commutable.
        return bool(posting_countries & candidate_countries)

    declared = {location.casefold() for location in candidate.locations}
    posting_places = {city.casefold() for city in posting.cities if city}
    if posting_places and declared:
        return any(
            place in declared or any(place in location for location in declared)
            for place in posting_places
        )
    return True


def _seniority_ok(
    candidate: CandidateSnapshot, posting: PostingSnapshot, config: GateConfig
) -> bool:
    """Only a *stated* minimum gates. An inferred one would be a guess with teeth.

    Over-qualification never gates: whether a senior engineer wants a mid-level
    role is their decision, and the seniority sub-score already reflects it.
    """
    if posting.min_years is None or candidate.years_experience is None:
        return True
    shortfall = posting.min_years - candidate.years_experience
    return shortfall <= config.max_years_shortfall


def _language_ok(candidate: CandidateSnapshot, posting: PostingSnapshot) -> bool:
    """Fails when a required language is undeclared or declared below the level asked."""
    if not posting.required_languages:
        return True

    declared = {ability.language.casefold(): ability.level for ability in candidate.languages}
    for required in posting.required_languages:
        held = declared.get(required.language.casefold())
        if held is None:
            return False
        if required.level and held < required.level:
            return False
    return True


def _freshness_ok(posting: PostingSnapshot, config: GateConfig, now: datetime) -> tuple[bool, bool]:
    """Returns (fresh_enough, verified_recently).

    Reported separately because they are different problems: an old posting may
    still be open, while an unverified one may be a dead link — the defect the
    product exists to avoid.
    """
    fresh = True
    if posting.posted_at is not None:
        age = now - posting.posted_at
        fresh = age <= timedelta(days=config.max_posting_age_days)

    if not config.require_verified_url:
        return fresh, True

    verified = posting.url_status is UrlStatus.LIVE
    if verified and posting.last_verified_at is not None:
        window = timedelta(hours=config.verification_window_hours)
        verified = (now - posting.last_verified_at) <= window
    return fresh, verified
