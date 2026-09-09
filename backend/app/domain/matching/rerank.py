"""Cross-encoder reranking (§4.2 stage 4).

A bi-encoder embeds query and document separately, so their vectors never see
each other; a cross-encoder scores the pair jointly and is markedly more
accurate at the cost of about 40 ms per pair on CPU. That cost is affordable
only because the funnel has already narrowed 3,200 postings to about 120.

The domain defines the protocol; `app.adapters.embeddings` implements it. The
`fixed` order below is what runs when no reranker is installed: retrieval order
is preserved and the sub-score is neutral, so the pipeline degrades to
"retrieval only" rather than to nonsense.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


class Reranker(Protocol):
    model_id: str

    def score_pairs(self, query: str, documents: Sequence[str]) -> list[float]: ...


@dataclass(frozen=True, slots=True)
class RerankedHit:
    posting_id: str
    score: float
    rank: int


def rerank(
    query: str,
    candidates: Sequence[tuple[str, str]],
    reranker: Reranker | None,
    *,
    top_n: int = 25,
) -> list[RerankedHit]:
    """Score `(posting_id, document)` pairs against the query and keep the top N.

    With no reranker configured the input order is preserved and every score is
    0.5 — `normalize_semantic` then returns a neutral value for the whole pool,
    so the semantic term contributes nothing rather than noise.
    """
    if not candidates:
        return []

    if reranker is None:
        return [
            RerankedHit(posting_id=posting_id, score=0.5, rank=index + 1)
            for index, (posting_id, _) in enumerate(candidates[:top_n])
        ]

    scores = reranker.score_pairs(query, [document for _, document in candidates])
    ordered = sorted(
        zip((posting_id for posting_id, _ in candidates), scores, strict=True),
        key=lambda pair: (-pair[1], pair[0]),
    )
    return [
        RerankedHit(posting_id=posting_id, score=round(float(score), 6), rank=index + 1)
        for index, (posting_id, score) in enumerate(ordered[:top_n])
    ]
