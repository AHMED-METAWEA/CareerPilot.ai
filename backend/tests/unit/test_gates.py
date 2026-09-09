"""Eligibility gates (§8.3).

Two principles under test: absence of evidence is not a failure, and anything a
candidate genuinely cannot do belongs in a gate rather than as a deduction
inside a score that a good skill match could outweigh.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.domain.matching.gates import GateConfig, GateFailure, evaluate_gates
from app.domain.models import (
    CandidateSnapshot,
    LanguageAbility,
    PostingSnapshot,
    RemoteType,
    Seniority,
    UrlStatus,
    WorkAuthorization,
)

NOW = datetime(2026, 9, 9, 12, tzinfo=UTC)
CONFIG = GateConfig()

CANDIDATE = CandidateSnapshot(
    profile_id="p1",
    years_experience=3,
    seniority_level=Seniority.MID,
    locations=("Cairo, Egypt",),
    countries=("EG",),
    work_authorization=(WorkAuthorization(country="EG", status="citizen"),),
    languages=(
        LanguageAbility(language="English", cefr="C1"),
        LanguageAbility(language="Arabic", cefr="native"),
    ),
    skills=frozenset({"Python"}),
)


def posting(**overrides: object) -> PostingSnapshot:
    base: dict[str, object] = {
        "posting_id": "j1",
        "title": "Data Engineer",
        "posted_at": NOW - timedelta(days=2),
        "url_status": UrlStatus.LIVE,
        "last_verified_at": NOW - timedelta(hours=3),
        "remote_type": RemoteType.REMOTE,
    }
    return PostingSnapshot(**{**base, **overrides})  # type: ignore[arg-type]


def failures(**overrides: object) -> set[GateFailure]:
    return set(evaluate_gates(CANDIDATE, posting(**overrides), CONFIG, now=NOW).failures)


def test_clean_posting_passes() -> None:
    assert evaluate_gates(CANDIDATE, posting(), CONFIG, now=NOW).passed


def test_silence_never_fails_a_gate() -> None:
    """Most postings say nothing about most things. Gating on silence would hide
    the corpus."""
    quiet = PostingSnapshot(
        posting_id="j2",
        title="Engineer",
        posted_at=NOW,
        url_status=UrlStatus.LIVE,
        last_verified_at=NOW,
    )
    assert evaluate_gates(CANDIDATE, quiet, CONFIG, now=NOW).passed


def test_work_authorisation() -> None:
    assert failures(requires_authorization_in=("DE",)) == {GateFailure.WORK_AUTHORIZATION}
    # Sponsorship offered means the candidate can take the role.
    assert failures(requires_authorization_in=("DE",), offers_sponsorship=True) == set()
    # Authorised where the role requires it.
    assert failures(requires_authorization_in=("EG",)) == set()


def test_location_gate_only_bites_where_presence_is_required() -> None:
    assert failures(remote_type=RemoteType.REMOTE, countries=("US",)) == set()
    assert failures(remote_type=RemoteType.ONSITE, countries=("DE",)) == {GateFailure.LOCATION}
    assert failures(remote_type=RemoteType.HYBRID, countries=("DE",)) == {GateFailure.LOCATION}
    assert failures(remote_type=RemoteType.ONSITE, countries=("EG",)) == set()


def test_candidate_not_open_to_remote() -> None:
    homebody = CANDIDATE.model_copy(update={"open_to_remote": False})
    result = evaluate_gates(homebody, posting(remote_type=RemoteType.REMOTE), CONFIG, now=NOW)
    assert GateFailure.LOCATION in result.failures


@pytest.mark.parametrize(
    ("min_years", "expected"),
    [(None, set()), (3.0, set()), (5.0, set()), (6.0, {GateFailure.SENIORITY_FLOOR})],
)
def test_seniority_floor_allows_a_two_year_stretch(
    min_years: float | None, expected: set[GateFailure]
) -> None:
    assert failures(min_years=min_years) == expected


def test_over_qualification_never_gates() -> None:
    """Whether a senior engineer wants a mid-level role is their decision."""
    senior = CANDIDATE.model_copy(update={"years_experience": 12.0})
    assert evaluate_gates(senior, posting(min_years=2), CONFIG, now=NOW).passed


def test_language_gate() -> None:
    german = (LanguageAbility(language="German", cefr="B2"),)
    assert failures(required_languages=german) == {GateFailure.LANGUAGE}
    # Declared above the level asked for.
    english = (LanguageAbility(language="English", cefr="B2"),)
    assert failures(required_languages=english) == set()
    # Declared, but below.
    high_english = (LanguageAbility(language="English", cefr="C2"),)
    assert failures(required_languages=high_english) == {GateFailure.LANGUAGE}


def test_freshness_and_verification_are_separate_failures() -> None:
    """An old posting may still be open; an unverified one may be a dead link."""
    assert failures(posted_at=NOW - timedelta(days=60)) == {GateFailure.FRESHNESS}
    assert failures(url_status=UrlStatus.UNKNOWN, last_verified_at=None) == {
        GateFailure.UNVERIFIED_URL
    }
    assert failures(last_verified_at=NOW - timedelta(hours=72)) == {GateFailure.UNVERIFIED_URL}


def test_verification_can_be_switched_off() -> None:
    config = GateConfig(require_verified_url=False)
    result = evaluate_gates(
        CANDIDATE, posting(url_status=UrlStatus.UNKNOWN, last_verified_at=None), config, now=NOW
    )
    assert result.passed


def test_every_failure_is_reported_with_a_reason() -> None:
    """A user asking why a role was withheld deserves the whole answer."""
    result = evaluate_gates(
        CANDIDATE,
        posting(
            remote_type=RemoteType.ONSITE,
            countries=("DE",),
            requires_authorization_in=("DE",),
            min_years=9,
            posted_at=NOW - timedelta(days=90),
        ),
        CONFIG,
        now=NOW,
    )
    assert len(result.failures) == 4
    assert all(reason and reason[0].isupper() for reason in result.reasons())
