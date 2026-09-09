"""Ranking and agreement metrics (§9.2).

Plain implementations over graded relevance labels (0–3), with the conventions
stated rather than assumed — most disagreements about a metric turn out to be
disagreements about a convention.

* **NDCG** uses the exponential gain `2^rel - 1`, which is what makes the
  difference between a grade-3 and a grade-2 result matter more than the
  difference between a 1 and a 0. Ideal DCG is computed over the *full* label
  set, so a system that cannot retrieve a known-relevant item is penalised for
  missing it rather than scored against what it happened to return.
* **Precision@k and recall** treat grade ≥ 2 as relevant. Grade 1 means "related
  but not worth applying to", and counting it as a hit would flatter every
  configuration equally.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

RELEVANT_GRADE = 2
"""Grade 2 or 3 is a role worth the two hours an application costs."""


def dcg(gains: Sequence[float]) -> float:
    return sum(gain / math.log2(index + 2) for index, gain in enumerate(gains))


def ndcg_at_k(ranked_ids: Sequence[str], labels: Mapping[str, int], k: int = 10) -> float:
    """Normalised discounted cumulative gain at cut-off `k`.

    Unlabelled results count as grade 0: an item nobody graded is not evidence
    of quality, and treating it as missing would reward a system for returning
    things the annotators never saw.
    """
    if not labels:
        return 0.0
    gains = [(2 ** labels.get(posting_id, 0)) - 1 for posting_id in ranked_ids[:k]]
    ideal = sorted(((2**grade) - 1 for grade in labels.values()), reverse=True)[:k]
    ideal_dcg = dcg(ideal)
    return round(dcg(gains) / ideal_dcg, 4) if ideal_dcg else 0.0


def mrr(ranked_ids: Sequence[str], labels: Mapping[str, int]) -> float:
    """Reciprocal rank of the first genuinely relevant result."""
    for index, posting_id in enumerate(ranked_ids, start=1):
        if labels.get(posting_id, 0) >= RELEVANT_GRADE:
            return round(1.0 / index, 4)
    return 0.0


def precision_at_k(ranked_ids: Sequence[str], labels: Mapping[str, int], k: int = 5) -> float:
    if k <= 0:
        return 0.0
    top = ranked_ids[:k]
    if not top:
        return 0.0
    hits = sum(1 for posting_id in top if labels.get(posting_id, 0) >= RELEVANT_GRADE)
    return round(hits / len(top), 4)


def recall_of_grade(
    ranked_ids: Sequence[str], labels: Mapping[str, int], *, grade: int = 3, k: int = 10
) -> float:
    """Share of the best roles that made it into the top `k`.

    The question this answers is the one that matters to a user: did the system
    surface the role they would actually have wanted?
    """
    targets = {posting_id for posting_id, value in labels.items() if value >= grade}
    if not targets:
        return 1.0  # nothing to find; not a failure
    found = sum(1 for posting_id in ranked_ids[:k] if posting_id in targets)
    return round(found / len(targets), 4)


@dataclass(frozen=True, slots=True)
class RankingMetrics:
    ndcg_at_10: float
    mrr: float
    precision_at_5: float
    recall_grade3_at_10: float
    queries: int

    def as_dict(self) -> dict[str, float | int]:
        return {
            "ndcg@10": self.ndcg_at_10,
            "mrr": self.mrr,
            "p@5": self.precision_at_5,
            "recall_grade3@10": self.recall_grade3_at_10,
            "queries": self.queries,
        }


def evaluate_rankings(
    rankings: Mapping[str, Sequence[str]], labels: Mapping[str, Mapping[str, int]]
) -> RankingMetrics:
    """Macro-average over queries (one query = one candidate profile).

    Macro, not micro: each candidate's experience counts once, so a profile with
    many labelled postings cannot dominate the headline number.
    """
    scored = [(rankings.get(query, []), query_labels) for query, query_labels in labels.items()]
    scored = [(ranked, query_labels) for ranked, query_labels in scored if query_labels]
    if not scored:
        return RankingMetrics(0.0, 0.0, 0.0, 0.0, 0)

    return RankingMetrics(
        ndcg_at_10=round(sum(ndcg_at_k(r, lbl, 10) for r, lbl in scored) / len(scored), 4),
        mrr=round(sum(mrr(r, lbl) for r, lbl in scored) / len(scored), 4),
        precision_at_5=round(sum(precision_at_k(r, lbl, 5) for r, lbl in scored) / len(scored), 4),
        recall_grade3_at_10=round(
            sum(recall_of_grade(r, lbl, grade=3, k=10) for r, lbl in scored) / len(scored), 4
        ),
        queries=len(scored),
    )


# ── Annotator agreement (§9.1) ────────────────────────────────────────


def cohens_kappa(first: Sequence[int], second: Sequence[int]) -> float:
    """Agreement between two annotators, corrected for chance.

    The gate in §9.1 is κ ≥ 0.60. Below that the rubric is defective and no
    metric derived from the labels can be trusted — the correct response is to
    revise the rubric, not to relabel until the number improves.
    """
    if len(first) != len(second):
        raise ValueError("annotator label lists must be the same length")
    if not first:
        return 0.0

    categories = sorted(set(first) | set(second))
    total = len(first)
    observed = sum(1 for a, b in zip(first, second, strict=True) if a == b) / total

    expected = 0.0
    for category in categories:
        p_first = sum(1 for value in first if value == category) / total
        p_second = sum(1 for value in second if value == category) / total
        expected += p_first * p_second

    if math.isclose(expected, 1.0):
        return 1.0 if math.isclose(observed, 1.0) else 0.0
    return round((observed - expected) / (1 - expected), 4)


def fleiss_kappa(ratings: Sequence[Sequence[int]]) -> float:
    """Agreement across three or more annotators.

    `ratings[i]` is every grade given to item i. Items rated by fewer than two
    annotators are skipped rather than counted as perfect agreement.
    """
    usable = [row for row in ratings if len(row) >= 2]
    if not usable:
        return 0.0
    categories = sorted({grade for row in usable for grade in row})
    if len(categories) < 2:
        return 1.0

    n_raters = len(usable[0])
    if any(len(row) != n_raters for row in usable):
        raise ValueError("fleiss_kappa requires the same number of ratings per item")

    counts = [
        [sum(1 for grade in row if grade == category) for category in categories] for row in usable
    ]
    p_item = [
        (sum(count * count for count in row) - n_raters) / (n_raters * (n_raters - 1))
        for row in counts
    ]
    p_bar = sum(p_item) / len(usable)
    p_category = [
        sum(row[index] for row in counts) / (len(usable) * n_raters)
        for index in range(len(categories))
    ]
    p_expected = sum(value * value for value in p_category)

    if math.isclose(p_expected, 1.0):
        return 1.0 if math.isclose(p_bar, 1.0) else 0.0
    return round((p_bar - p_expected) / (1 - p_expected), 4)
