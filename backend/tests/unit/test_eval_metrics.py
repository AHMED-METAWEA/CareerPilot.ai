"""Ranking and agreement metrics (§9.2).

Most disagreements about a metric turn out to be disagreements about a
convention, so the conventions are what these tests pin.
"""

from __future__ import annotations

import pytest

from app.eval.metrics import (
    cohens_kappa,
    evaluate_rankings,
    fleiss_kappa,
    mrr,
    ndcg_at_k,
    precision_at_k,
    recall_of_grade,
)

LABELS = {"a": 3, "b": 2, "c": 1, "d": 0, "e": 3}


def test_perfect_ranking_scores_one() -> None:
    assert ndcg_at_k(["a", "e", "b", "c", "d"], LABELS, k=10) == 1.0


def test_reversed_ranking_scores_far_below_one() -> None:
    assert ndcg_at_k(["d", "c", "b", "e", "a"], LABELS, k=10) < 0.7


def test_ideal_dcg_covers_the_whole_label_set() -> None:
    """A system that cannot retrieve a known-relevant item is penalised for
    missing it, not scored against whatever it happened to return."""
    partial = ndcg_at_k(["b", "c"], LABELS, k=10)
    complete = ndcg_at_k(["a", "e", "b", "c"], LABELS, k=10)
    assert partial < complete


def test_unlabelled_results_count_as_zero() -> None:
    """An item nobody graded is not evidence of quality."""
    assert ndcg_at_k(["unlabelled", "a", "e"], LABELS, k=10) < ndcg_at_k(["a", "e"], LABELS, k=10)


def test_exponential_gain_rewards_the_best_result() -> None:
    """2^rel - 1: the gap between a 3 and a 2 must matter more than 1 versus 0."""
    labels = {"top": 3, "ok": 2}
    assert ndcg_at_k(["top", "ok"], labels, k=2) > ndcg_at_k(["ok", "top"], labels, k=2)


def test_mrr_finds_the_first_relevant_result() -> None:
    assert mrr(["d", "c", "b"], LABELS) == pytest.approx(1 / 3, abs=1e-3)
    assert mrr(["a"], LABELS) == 1.0
    assert mrr(["d", "c"], LABELS) == 0.0  # grade 1 is not "relevant"


def test_grade_one_is_not_a_hit() -> None:
    """Grade 1 means 'related but not worth applying to'. Counting it as a hit
    would flatter every configuration equally."""
    assert precision_at_k(["c", "c", "c"], LABELS, k=3) == 0.0
    assert precision_at_k(["a", "b", "c"], LABELS, k=3) == pytest.approx(2 / 3, abs=1e-3)


def test_recall_of_the_best_roles() -> None:
    """The question a user actually has: did the best role surface?"""
    assert recall_of_grade(["a", "d"], LABELS, grade=3, k=10) == 0.5
    assert recall_of_grade(["a", "e"], LABELS, grade=3, k=10) == 1.0
    assert recall_of_grade(["a"], {"a": 1}, grade=3, k=10) == 1.0  # nothing to find


def test_evaluate_rankings_macro_averages_over_queries() -> None:
    """One candidate with many labelled postings must not dominate the headline."""
    labels = {"p1": {"a": 3}, "p2": {"x": 3, "y": 3, "z": 3}}
    rankings = {"p1": ["a"], "p2": ["q", "q2", "q3"]}
    metrics = evaluate_rankings(rankings, labels)
    assert metrics.queries == 2
    assert metrics.ndcg_at_10 == pytest.approx(0.5)


def test_empty_inputs() -> None:
    assert ndcg_at_k([], LABELS) == 0.0
    assert ndcg_at_k(["a"], {}) == 0.0
    assert evaluate_rankings({}, {}).queries == 0


def test_cohens_kappa() -> None:
    assert cohens_kappa([0, 1, 2, 3], [0, 1, 2, 3]) == 1.0
    assert cohens_kappa([0, 1, 2, 3], [3, 2, 1, 0]) < 0
    assert cohens_kappa([], []) == 0.0
    with pytest.raises(ValueError, match="same length"):
        cohens_kappa([1, 2], [1])


def test_kappa_corrects_for_chance() -> None:
    """Two annotators who almost always say the same thing agree by chance."""
    mostly = cohens_kappa([3] * 9 + [0], [3] * 9 + [3])
    assert mostly < 0.6, "near-constant labels should not clear the gate"


def test_fleiss_kappa_across_three_annotators() -> None:
    assert fleiss_kappa([[3, 3, 3], [0, 0, 0], [2, 2, 2]]) == 1.0
    assert fleiss_kappa([[3, 0, 2], [0, 3, 1], [2, 1, 3]]) < 0.6
    assert fleiss_kappa([]) == 0.0
