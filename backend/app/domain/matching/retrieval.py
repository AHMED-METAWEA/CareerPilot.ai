"""Hybrid retrieval and Reciprocal Rank Fusion (§7.3).

    RRF(d) = Σ  1 / (k + rank_r(d)),   k = 60
            r∈{bm25, vector}

Two retrievers, fused by rank rather than by score. Fusing scores would require
the two to be commensurable, and a BM25 score and a cosine similarity are not —
one is unbounded and corpus-dependent, the other is a bounded angle.

Neither retriever is sufficient alone, which is the point of running both:

* **Lexical** catches exact tool and framework names. `PyTorch` and
  `TensorFlow` sit close together in embedding space and are categorically
  different in a requirement.
* **Vector** catches paraphrase and cross-lingual equivalence: "معالجة اللغة
  الطبيعية" and "NLP" share no tokens at all.

§9.3's ablation measures what each contributes, rather than assuming.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

DEFAULT_K = 60
"""The constant from the original RRF paper. It damps the head of each list so a
single retriever's top hit cannot dominate a fused ranking on its own."""


@dataclass(frozen=True, slots=True)
class RetrievalHit:
    posting_id: str
    rank: int
    score: float = 0.0
    """The retriever's own score. Carried for diagnostics; never fused directly."""


@dataclass(frozen=True, slots=True)
class FusedHit:
    posting_id: str
    fused_score: float
    ranks: dict[str, int] = field(default_factory=dict)
    """Which retriever found it, and where. This is what makes an ablation readable."""

    @property
    def retrievers(self) -> tuple[str, ...]:
        return tuple(sorted(self.ranks))


def reciprocal_rank_fusion(
    rankings: Mapping[str, Sequence[RetrievalHit]],
    *,
    k: int = DEFAULT_K,
    limit: int | None = None,
) -> list[FusedHit]:
    """Fuse ranked lists from independent retrievers.

    Ties break on posting id so that a run is reproducible: two documents with
    identical fused scores must not swap places between runs, or the evaluation
    harness measures noise.
    """
    contributions: dict[str, float] = {}
    ranks: dict[str, dict[str, int]] = {}

    for retriever, hits in rankings.items():
        for hit in hits:
            contributions[hit.posting_id] = contributions.get(hit.posting_id, 0.0) + 1.0 / (
                k + hit.rank
            )
            ranks.setdefault(hit.posting_id, {})[retriever] = hit.rank

    fused = [
        FusedHit(posting_id=posting_id, fused_score=round(score, 6), ranks=ranks[posting_id])
        for posting_id, score in contributions.items()
    ]
    fused.sort(key=lambda hit: (-hit.fused_score, hit.posting_id))
    return fused[:limit] if limit else fused


def rank_hits(
    posting_ids: Sequence[str], scores: Sequence[float] | None = None
) -> list[RetrievalHit]:
    """Turn an ordered list of ids into ranked hits (rank 1 is best)."""
    scores = scores or [0.0] * len(posting_ids)
    return [
        RetrievalHit(posting_id=posting_id, rank=index + 1, score=score)
        for index, (posting_id, score) in enumerate(zip(posting_ids, scores, strict=False))
    ]
