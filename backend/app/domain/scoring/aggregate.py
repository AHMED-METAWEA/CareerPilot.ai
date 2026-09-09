"""Score aggregation (§8.2).

    gate  = Π gate_i                       # boolean; any failure ⇒ score 0
    score = gate × ( 0.35 · skill_coverage
                   + 0.25 · requirement_alignment
                   + 0.15 · seniority_fit
                   + 0.15 · semantic_similarity
                   + 0.10 · freshness )

Weights come from `config/config.yaml` and are validated to sum to 1.0 at load.
The output carries its own decomposition, so `/matches/{id}` can show which
term produced the number rather than asserting it.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any

from app.domain.matching.gates import GateFailure, GateResult


@dataclass(frozen=True, slots=True)
class ScoreWeights:
    skill_coverage: float = 0.35
    requirement_alignment: float = 0.25
    seniority_fit: float = 0.15
    semantic_similarity: float = 0.15
    freshness: float = 0.10

    def total(self) -> float:
        return (
            self.skill_coverage
            + self.requirement_alignment
            + self.seniority_fit
            + self.semantic_similarity
            + self.freshness
        )


@dataclass(frozen=True, slots=True)
class SubScores:
    skill_coverage: float
    requirement_alignment: float
    seniority_fit: float
    semantic_similarity: float
    freshness: float

    def as_dict(self) -> dict[str, float]:
        return {
            "skill_coverage": self.skill_coverage,
            "requirement_alignment": self.requirement_alignment,
            "seniority_fit": self.seniority_fit,
            "semantic_similarity": self.semantic_similarity,
            "freshness": self.freshness,
        }


@dataclass(frozen=True, slots=True)
class MatchScore:
    """A score plus the reason it is what it is."""

    total: float
    gate_passed: bool
    gate_failures: tuple[GateFailure, ...]
    subscores: SubScores
    contributions: dict[str, float] = field(default_factory=dict)
    """Each term's weighted contribution — what actually moved the number."""
    unavailable: tuple[str, ...] = ()
    """Terms that could not be computed for this posting."""
    assessed_weight: float = 1.0
    """Share of the total weight this score is based on. Below 1.0, the posting
    could only be partly assessed and cannot reach the top of a list."""

    def explain(self) -> dict[str, Any]:
        """The decomposition, for the API and the evidence view."""
        return {
            "total": self.total,
            "gate_passed": self.gate_passed,
            "gate_failures": [failure.value for failure in self.gate_failures],
            "subscores": self.subscores.as_dict(),
            "contributions": self.contributions,
            "unavailable": list(self.unavailable),
            "assessed_weight": self.assessed_weight,
            "top_factor": (
                max(self.contributions, key=lambda key: self.contributions[key])
                if self.contributions
                else None
            ),
        }


def aggregate(
    subscores: SubScores,
    gate: GateResult,
    weights: ScoreWeights | None = None,
    *,
    unavailable: Collection[str] = (),
) -> MatchScore:
    """Combine sub-scores behind the gate.

    A failed gate is a hard zero, not a penalty. That is the whole point of
    having gates: no amount of skill overlap makes a role the candidate cannot
    legally take into a good match.

    `unavailable` names terms that could not be computed for this posting —
    most often because no requirements could be extracted from its description.
    Those terms contribute nothing, and the weights are **not** renormalised:
    a posting scores on what we could actually verify about it.

    Both alternatives were tried against the real corpus and are worse:

    * Scoring the missing terms as 1.0 hands an unparseable posting 60% of the
      weight for telling us nothing — a badly formatted ad outranks a good match.
    * Renormalising the remaining weights lets the same posting compete at full
      strength on freshness and seniority alone, which put "Accounting Manager"
      above every engineering role for a data engineer.

    Capping at the weight we could assess says the honest thing: we cannot
    recommend a posting we could not read, and `assessed_weight` reports how
    much of the score was measurable so a client can say so too.
    """
    weights = weights or ScoreWeights()

    if not gate.passed:
        return MatchScore(
            total=0.0,
            gate_passed=False,
            gate_failures=gate.failures,
            subscores=subscores,
            contributions={},
        )

    available = {
        "skill_coverage": (weights.skill_coverage, subscores.skill_coverage),
        "requirement_alignment": (weights.requirement_alignment, subscores.requirement_alignment),
        "seniority_fit": (weights.seniority_fit, subscores.seniority_fit),
        "semantic_similarity": (weights.semantic_similarity, subscores.semantic_similarity),
        "freshness": (weights.freshness, subscores.freshness),
    }
    for term in unavailable:
        available.pop(term, None)

    assessed_weight = sum(weight for weight, _ in available.values())
    contributions = {term: round(weight * value, 4) for term, (weight, value) in available.items()}
    total = round(sum(contributions.values()), 4)
    return MatchScore(
        total=min(total, 1.0),
        gate_passed=True,
        gate_failures=(),
        subscores=subscores,
        contributions=contributions,
        unavailable=tuple(sorted(unavailable)),
        assessed_weight=round(assessed_weight, 4),
    )
