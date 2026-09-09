"""Negative controls (§9.4).

These need no human labels, which makes them the one part of the evaluation
that can run from the day the pipeline exists. They do not measure how good the
ranking is; they check that it is measuring fit at all:

* **Cross-domain separation.** A backend engineering CV scored against nursing
  and legal postings. Weak separation means the scorer is reading writing style,
  not fit — the failure mode that makes a demo look good and a product useless.
* **Seniority monotonicity.** Identical postings differing only in stated
  seniority. The score must move in one direction.
* **Shuffled pairings.** Scores for true CV–posting pairs against scores for
  randomly reassigned ones. If the distributions overlap, the score is noise.

A control that fails is not a tuning opportunity. It means the number the rest
of the evaluation rests on does not mean what it claims.
"""

from __future__ import annotations

import random
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from itertools import pairwise

from app.domain.models import CandidateSnapshot, PostingSnapshot

Scorer = Callable[[CandidateSnapshot, PostingSnapshot], float]


@dataclass(frozen=True, slots=True)
class ControlResult:
    name: str
    passed: bool
    detail: str
    values: dict[str, float] = field(default_factory=dict)


MIN_DOMAIN_SEPARATION = 0.15
"""In-domain mean minus out-of-domain mean. Below this the scorer is not
distinguishing a data engineer from a nurse, whatever its NDCG says."""


def cross_domain_separation(
    candidate: CandidateSnapshot,
    in_domain: Sequence[PostingSnapshot],
    out_of_domain: Sequence[PostingSnapshot],
    scorer: Scorer,
) -> ControlResult:
    if not in_domain or not out_of_domain:
        return ControlResult("cross_domain", False, "no postings supplied")

    inside = statistics.fmean(scorer(candidate, posting) for posting in in_domain)
    outside = statistics.fmean(scorer(candidate, posting) for posting in out_of_domain)
    separation = inside - outside
    return ControlResult(
        name="cross_domain",
        passed=separation >= MIN_DOMAIN_SEPARATION,
        detail=(
            f"in-domain mean {inside:.3f}, out-of-domain mean {outside:.3f}, "
            f"separation {separation:.3f} (needs ≥ {MIN_DOMAIN_SEPARATION})"
        ),
        values={
            "in_domain": round(inside, 4),
            "out_of_domain": round(outside, 4),
            "separation": round(separation, 4),
        },
    )


def seniority_monotonicity(
    candidate: CandidateSnapshot, ladder: Sequence[PostingSnapshot], scorer: Scorer
) -> ControlResult:
    """`ladder` is one posting repeated at increasing seniority levels."""
    scores = [scorer(candidate, posting) for posting in ladder]
    if len(scores) < 3:
        return ControlResult("seniority_monotonicity", False, "need at least three levels")

    peak = scores.index(max(scores))
    rising = all(a <= b + 1e-9 for a, b in pairwise(scores[: peak + 1]))
    falling = all(a >= b - 1e-9 for a, b in pairwise(scores[peak:]))
    return ControlResult(
        name="seniority_monotonicity",
        passed=rising and falling,
        detail=(
            f"scores {[round(score, 3) for score in scores]} peak at index {peak}; "
            "expected a single peak with no reversals"
        ),
        values={f"level_{index}": round(score, 4) for index, score in enumerate(scores)},
    )


MIN_SHUFFLE_SEPARATION = 0.10


def shuffled_pairings(
    pairs: Sequence[tuple[CandidateSnapshot, PostingSnapshot]],
    scorer: Scorer,
    *,
    seed: int = 20260909,
) -> ControlResult:
    """True pairings against randomly reassigned ones.

    Seeded, because a control that reports a different answer each run cannot
    gate a build.
    """
    if len(pairs) < 4:
        return ControlResult("shuffled_pairings", False, "need at least four pairs")

    true_scores = [scorer(candidate, posting) for candidate, posting in pairs]

    rng = random.Random(seed)
    postings = [posting for _, posting in pairs]
    shuffled = postings[:]
    for _ in range(10):
        rng.shuffle(shuffled)
        if all(a is not b for a, b in zip(postings, shuffled, strict=True)):
            break
    false_scores = [
        scorer(candidate, posting) for (candidate, _), posting in zip(pairs, shuffled, strict=True)
    ]

    true_mean = statistics.fmean(true_scores)
    false_mean = statistics.fmean(false_scores)
    separation = true_mean - false_mean
    return ControlResult(
        name="shuffled_pairings",
        passed=separation >= MIN_SHUFFLE_SEPARATION,
        detail=(
            f"true mean {true_mean:.3f}, shuffled mean {false_mean:.3f}, "
            f"separation {separation:.3f} (needs ≥ {MIN_SHUFFLE_SEPARATION})"
        ),
        values={
            "true_mean": round(true_mean, 4),
            "shuffled_mean": round(false_mean, 4),
            "separation": round(separation, 4),
        },
    )


def summarise(results: Sequence[ControlResult]) -> dict[str, object]:
    return {
        "passed": all(result.passed for result in results),
        "controls": [
            {
                "name": result.name,
                "passed": result.passed,
                "detail": result.detail,
                "values": result.values,
            }
            for result in results
        ],
    }
