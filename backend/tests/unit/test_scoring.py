"""Sub-scores, aggregation and presentation (§8.2, §8.4, §8.5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.domain.matching.gates import GateFailure, GateResult
from app.domain.models import PostingRequirement, Seniority
from app.domain.scoring.aggregate import ScoreWeights, SubScores, aggregate
from app.domain.scoring.percentile import (
    percentile_of,
    violates_presentation_rules,
)
from app.domain.scoring.subscores import (
    freshness,
    normalize_semantic,
    requirement_alignment,
    seniority_fit,
    skill_coverage,
)

NOW = datetime(2026, 9, 9, tzinfo=UTC)
PASSED = GateResult(passed=True, failures=())


def requirement(skill: str, *, must: bool = False, text: str | None = None) -> PostingRequirement:
    return PostingRequirement(
        text=text or f"Experience with {skill}", kind="skill", is_must_have=must, skill=skill
    )


# ── skill coverage ────────────────────────────────────────────────────


def test_must_haves_weigh_triple() -> None:
    """A nice-to-have is a preference; a must-have is a filter."""
    reqs = [requirement("Python", must=True), requirement("Terraform")]
    with_must = skill_coverage(frozenset({"Python"}), reqs)
    with_nice = skill_coverage(frozenset({"Terraform"}), reqs)
    assert with_must.score > with_nice.score
    assert with_must.score == 0.75 and with_nice.score == 0.25


def test_coverage_is_reported_as_a_fraction_not_a_percentage() -> None:
    coverage = skill_coverage(frozenset({"Python"}), [requirement("Python"), requirement("Go")])
    assert coverage.fraction == "1/2"
    assert coverage.missing == ("Go",)


def test_missing_must_haves_are_singled_out() -> None:
    coverage = skill_coverage(frozenset(), [requirement("Python", must=True), requirement("Go")])
    assert coverage.missing_must_haves == ("Python",)


def test_a_posting_with_no_skill_requirements_is_neutral_not_zero() -> None:
    coverage = skill_coverage(frozenset({"Python"}), [])
    assert coverage.score == 1.0 and coverage.total_count == 0


# ── requirement alignment ─────────────────────────────────────────────


def overlap(a: str, b: str) -> float:
    left, right = set(a.casefold().split()), set(b.casefold().split())
    return len(left & right) / len(left | right) if left | right else 0.0


def test_alignment_returns_the_evidence_it_scored_on() -> None:
    """The bullet that matched is what the user is shown, so the score and the
    explanation come from the same computation."""
    reqs = [requirement("Kafka", text="Built Kafka streaming pipelines")]
    bullets = ["Built Kafka streaming pipelines at scale", "Wrote quarterly reports"]
    alignment = requirement_alignment(reqs, bullets, overlap)
    assert alignment.evidence[0].best_bullet == "Built Kafka streaming pipelines at scale"
    assert alignment.evidence[0].status in {"met", "partial"}
    assert alignment.score > 0


def test_weak_evidence_is_not_presented_as_evidence() -> None:
    """A bullet that barely resembles the requirement is not a justification.

    Showing it would put a claim in front of the user that the score itself does
    not support — the failure mode this whole design exists to avoid.
    """
    reqs = [requirement("Kafka", text="Deep experience operating Kafka clusters in production")]
    alignment = requirement_alignment(reqs, ["Wrote quarterly reports"], overlap)
    assert alignment.evidence[0].status == "missing"
    assert alignment.evidence[0].best_bullet is None


def test_alignment_with_no_bullets_is_zero_and_says_missing() -> None:
    alignment = requirement_alignment([requirement("Kafka")], [], overlap)
    assert alignment.score == 0.0
    assert alignment.evidence[0].status == "missing"


def test_alignment_with_no_requirements_is_neutral() -> None:
    assert requirement_alignment([], ["anything"], overlap).score == 1.0


# ── seniority ─────────────────────────────────────────────────────────


def test_under_qualification_is_penalised_about_twice_as_hard() -> None:
    under, _ = seniority_fit(Seniority.JUNIOR, Seniority.SENIOR)
    over, _ = seniority_fit(Seniority.SENIOR, Seniority.JUNIOR)
    assert under < over
    assert (1 - under) == pytest.approx(2 * (1 - over), rel=0.01)


def test_exact_match_scores_one_and_unknown_is_neutral() -> None:
    assert seniority_fit(Seniority.MID, Seniority.MID)[0] == 1.0
    assert 0.5 < seniority_fit(None, Seniority.MID)[0] < 1.0


def test_seniority_moves_monotonically() -> None:
    """§9.4's negative control: identical postings differing only in stated
    seniority must score monotonically."""
    ladder = [Seniority.JUNIOR, Seniority.MID, Seniority.SENIOR, Seniority.STAFF]
    scores = [seniority_fit(Seniority.JUNIOR, level)[0] for level in ladder]
    assert scores == sorted(scores, reverse=True)


# ── freshness and semantic normalisation ──────────────────────────────


def test_freshness_halves_every_seven_days() -> None:
    assert freshness(NOW, now=NOW) == 1.0
    assert freshness(NOW - timedelta(days=7), now=NOW) == pytest.approx(0.5)
    assert freshness(NOW - timedelta(days=14), now=NOW) == pytest.approx(0.25)
    assert freshness(None, now=NOW) == 0.5


def test_semantic_normalisation_is_within_pool() -> None:
    """Cross-encoder logits mean nothing in absolute terms across queries."""
    assert normalize_semantic(5.0, [1.0, 3.0, 5.0]) == 1.0
    assert normalize_semantic(1.0, [1.0, 3.0, 5.0]) == 0.0
    assert normalize_semantic(3.0, [3.0, 3.0]) == 0.5  # degenerate pool


# ── aggregation ───────────────────────────────────────────────────────


def subscores(**overrides: float) -> SubScores:
    base = {
        "skill_coverage": 0.8,
        "requirement_alignment": 0.7,
        "seniority_fit": 1.0,
        "semantic_similarity": 0.6,
        "freshness": 0.9,
    }
    return SubScores(**{**base, **overrides})  # type: ignore[arg-type]


def test_a_failed_gate_is_a_hard_zero() -> None:
    """No amount of skill overlap makes an unusable role a good match."""
    score = aggregate(
        subscores(), GateResult(passed=False, failures=(GateFailure.WORK_AUTHORIZATION,))
    )
    assert score.total == 0.0 and not score.gate_passed


def test_weights_sum_to_one_so_a_perfect_match_scores_one() -> None:
    perfect = SubScores(1.0, 1.0, 1.0, 1.0, 1.0)
    assert aggregate(perfect, PASSED).total == pytest.approx(1.0)
    assert ScoreWeights().total() == pytest.approx(1.0)


def test_contributions_explain_the_number() -> None:
    score = aggregate(subscores(), PASSED)
    assert score.total == pytest.approx(sum(score.contributions.values()))
    assert score.explain()["top_factor"] in score.contributions


def test_unavailable_terms_are_dropped_not_scored_as_perfect() -> None:
    """Regression: a posting whose requirements could not be extracted scored
    1.0 on skill coverage and requirement alignment — 60% of the weight — for
    telling us nothing, and outranked genuinely good matches."""
    blind = subscores(skill_coverage=1.0, requirement_alignment=1.0)
    naive = aggregate(blind, PASSED)
    honest = aggregate(blind, PASSED, unavailable={"skill_coverage", "requirement_alignment"})
    assert honest.total < naive.total
    assert set(honest.contributions) == {"seniority_fit", "semantic_similarity", "freshness"}
    assert honest.unavailable == ("requirement_alignment", "skill_coverage")


def test_a_partly_assessable_posting_cannot_reach_the_top() -> None:
    """A posting we could only half read cannot outscore one we fully assessed.

    Renormalising the remaining weights was tried and is worse: it let
    "Accounting Manager" outrank every engineering role for a data engineer,
    because freshness and seniority alone carried it at full strength.
    """
    perfect = SubScores(1.0, 1.0, 1.0, 1.0, 1.0)
    full = aggregate(perfect, PASSED)
    partial = aggregate(perfect, PASSED, unavailable={"skill_coverage"})

    assert full.total == pytest.approx(1.0)
    assert partial.total == pytest.approx(0.65)
    assert partial.assessed_weight == pytest.approx(0.65)


# ── presentation ──────────────────────────────────────────────────────


def test_percentile_describes_the_pool_not_a_probability() -> None:
    pool = [index / 100 for index in range(100)]
    percentile = percentile_of(0.96, pool)
    assert percentile.band == "top 5%"
    summary = percentile.describe()
    assert "roles reviewed for you" in summary
    assert violates_presentation_rules(summary) == []


def test_forbidden_phrasings_are_caught() -> None:
    """ADR 0006: a score is never a probability of being hired or interviewed."""
    assert violates_presentation_rules("You have a 78% chance of getting this role")
    assert violates_presentation_rules("Probability of an interview: high")


def test_identical_scores_do_not_all_become_top_one_percent() -> None:
    assert percentile_of(0.5, [0.5] * 10).band == "lower half"


def test_empty_pool() -> None:
    assert percentile_of(0.9, []).pool_size == 0
