"""Percentile presentation (§8.5, ADR 0006).

Two rules, enforced here rather than left to whoever writes the UI:

1. A score is never described as a probability of being hired, interviewed, or
   passing a screen. Nothing in this system is calibrated against outcomes, and
   nothing could be (ADR 0004).
2. A score is presented as a percentile within the candidate's own reviewed
   pool — "top 4% of the 1,240 roles reviewed for you this week" — not as an
   absolute figure.

Percentiles are self-calibrating: as the corpus or the scorer changes, "top 4%"
keeps meaning the same thing, while "78%" quietly does not.
"""

from __future__ import annotations

import bisect
from collections.abc import Sequence
from dataclasses import dataclass

BANDS: tuple[tuple[float, str], ...] = (
    (99.0, "top 1%"),
    (95.0, "top 5%"),
    (90.0, "top 10%"),
    (75.0, "top 25%"),
    (50.0, "top half"),
)


@dataclass(frozen=True, slots=True)
class Percentile:
    value: float
    """0–100. The share of the pool this match scored at or above."""
    pool_size: int
    rank: int

    @property
    def band(self) -> str:
        for threshold, label in BANDS:
            if self.value >= threshold:
                return label
        return "lower half"

    def describe(self, *, window: str = "this week") -> str:
        """The sentence a user sees. Never a probability, by construction."""
        return f"{self.band} of the {self.pool_size:,} roles reviewed for you {window}"


def percentile_of(score: float, pool: Sequence[float]) -> Percentile:
    """Where `score` sits within `pool`.

    Ties take the lower percentile, so a pool of identical scores reports the
    honest "top half" rather than promoting every member to the top 1%.
    """
    if not pool:
        return Percentile(value=0.0, pool_size=0, rank=0)

    ordered = sorted(pool)
    below = bisect.bisect_left(ordered, score)
    value = round(100.0 * below / len(ordered), 2)
    rank = len(ordered) - bisect.bisect_right(ordered, score) + 1
    return Percentile(value=value, pool_size=len(ordered), rank=max(rank, 1))


def rank_matches(scores: Sequence[float]) -> list[Percentile]:
    """Percentiles for a whole pool, ordered as given."""
    return [percentile_of(score, scores) for score in scores]


FORBIDDEN_PHRASES = (
    "chance of getting",
    "chance of being hired",
    "probability of",
    "likelihood of an offer",
    "you will get",
    "guaranteed",
    "% chance",
)


def violates_presentation_rules(text: str) -> list[str]:
    """Guard for generated copy: any phrasing that reads as a hire probability.

    Used in tests and before any generated explanation is stored, so rule 1
    above is enforced mechanically rather than remembered.
    """
    lowered = text.casefold()
    return [phrase for phrase in FORBIDDEN_PHRASES if phrase in lowered]
