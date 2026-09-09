"""Negative controls (§9.4).

These do not measure how good the ranking is. They check that it is measuring
fit at all — and a failure is not a tuning opportunity, it means the number the
rest of the evaluation rests on does not mean what it claims.
"""

from __future__ import annotations

from app.domain.models import CandidateSnapshot, PostingSnapshot, Seniority
from app.eval.negative_controls import (
    cross_domain_separation,
    seniority_monotonicity,
    shuffled_pairings,
    summarise,
)

ENGINEER = CandidateSnapshot(profile_id="engineer", skills=frozenset({"Python", "SQL"}))
NURSE = CandidateSnapshot(profile_id="nurse", skills=frozenset({"Communication"}))


def posting(identifier: str, title: str, seniority: Seniority | None = None) -> PostingSnapshot:
    return PostingSnapshot(posting_id=identifier, title=title, seniority_level=seniority)


ENGINEERING_ROLES = [posting(f"e{index}", "Data Engineer") for index in range(4)]
NURSING_ROLES = [posting(f"n{index}", "Registered Nurse") for index in range(4)]


def domain_aware(candidate: CandidateSnapshot, job: PostingSnapshot) -> float:
    """A scorer that genuinely distinguishes disciplines."""
    engineering = "Engineer" in job.title
    return 0.9 if (candidate.profile_id == "engineer") == engineering else 0.2


def style_only(candidate: CandidateSnapshot, job: PostingSnapshot) -> float:
    """A scorer reading writing style rather than fit — the failure mode §9.4
    exists to catch, and the one that makes a demo look convincing."""
    return 0.6 + 0.01 * len(job.title)


def test_a_domain_aware_scorer_separates() -> None:
    result = cross_domain_separation(ENGINEER, ENGINEERING_ROLES, NURSING_ROLES, domain_aware)
    assert result.passed
    assert result.values["separation"] > 0.15


def test_a_style_only_scorer_is_caught() -> None:
    result = cross_domain_separation(ENGINEER, ENGINEERING_ROLES, NURSING_ROLES, style_only)
    assert not result.passed
    assert "separation" in result.detail


def test_control_reports_missing_data_rather_than_failing_silently() -> None:
    result = cross_domain_separation(ENGINEER, [], NURSING_ROLES, domain_aware)
    assert not result.passed and "no postings" in result.detail


def test_seniority_must_move_in_one_direction() -> None:
    ladder = [posting("l", "Data Engineer", level) for level in Seniority]

    def peaked(_: CandidateSnapshot, job: PostingSnapshot) -> float:
        order = list(Seniority)
        distance = abs(order.index(job.seniority_level or Seniority.MID) - 2)
        return 1.0 - 0.2 * distance

    assert seniority_monotonicity(ENGINEER, ladder, peaked).passed


def test_a_reversal_in_the_seniority_ladder_fails() -> None:
    ladder = [posting("l", "Data Engineer", level) for level in Seniority]
    scores = iter([0.5, 0.9, 0.4, 0.8, 0.3, 0.7])

    def erratic(_: CandidateSnapshot, __: PostingSnapshot) -> float:
        return next(scores)

    assert not seniority_monotonicity(ENGINEER, ladder, erratic).passed


def test_shuffled_pairings_separate_when_the_scorer_works() -> None:
    pairs = [(ENGINEER, job) for job in ENGINEERING_ROLES] + [(NURSE, job) for job in NURSING_ROLES]
    result = shuffled_pairings(pairs, domain_aware)
    assert result.passed
    assert result.values["true_mean"] > result.values["shuffled_mean"]


def test_shuffled_pairings_catch_a_scorer_that_ignores_the_candidate() -> None:
    """If reassigning candidates changes nothing, the score is not about fit."""
    pairs = [(ENGINEER, job) for job in ENGINEERING_ROLES] + [(NURSE, job) for job in NURSING_ROLES]
    assert not shuffled_pairings(pairs, style_only).passed


def test_shuffled_pairings_is_deterministic() -> None:
    """A control that reports a different answer each run cannot gate a build."""
    pairs = [(ENGINEER, job) for job in ENGINEERING_ROLES] + [(NURSE, job) for job in NURSING_ROLES]
    first = shuffled_pairings(pairs, domain_aware)
    second = shuffled_pairings(pairs, domain_aware)
    assert first.values == second.values


def test_summary_fails_if_any_control_fails() -> None:
    passing = cross_domain_separation(ENGINEER, ENGINEERING_ROLES, NURSING_ROLES, domain_aware)
    failing = cross_domain_separation(ENGINEER, ENGINEERING_ROLES, NURSING_ROLES, style_only)
    assert summarise([passing])["passed"] is True
    assert summarise([passing, failing])["passed"] is False
