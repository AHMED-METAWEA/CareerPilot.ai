"""Backend selection.

One configuration value decides whether the system runs on real model weights
or on the deterministic development backend. Nothing else in the codebase knows
which it got.
"""

from __future__ import annotations

import structlog

from app.adapters.embeddings.base import EmbeddingBackend
from app.adapters.embeddings.deterministic import DeterministicEmbedder, DeterministicReranker
from app.domain.matching.rerank import Reranker

log = structlog.get_logger(__name__)


def build_embedder(model_id: str) -> EmbeddingBackend:
    """Load the configured embedding model, or fall back with a loud warning.

    The fallback is deliberate and visible: a missing `[ml]` install should not
    stop the pipeline in development, and must never be mistaken for the real
    model in production — `job_embeddings.model` records which one wrote a
    vector, so a fallback run cannot silently pollute the index (R7).
    """
    if model_id in {"deterministic", DeterministicEmbedder.model_id}:
        return DeterministicEmbedder()

    try:
        from app.adapters.embeddings.local_st import SentenceTransformerEmbedder

        return SentenceTransformerEmbedder(model_id)
    except ImportError as exc:
        log.warning(
            "embeddings.falling_back_to_deterministic",
            requested=model_id,
            reason=str(exc),
            impact="lexical similarity only; cross-lingual matching will not work",
        )
        return DeterministicEmbedder()


def build_reranker(model_id: str) -> Reranker:
    if model_id in {"deterministic", DeterministicReranker.model_id}:
        return DeterministicReranker()

    try:
        from app.adapters.embeddings.local_st import CrossEncoderReranker

        return CrossEncoderReranker(model_id)
    except ImportError as exc:
        log.warning("rerank.falling_back_to_deterministic", requested=model_id, reason=str(exc))
        return DeterministicReranker()
