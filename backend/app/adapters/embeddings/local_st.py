"""Local sentence-transformers backend (§7.1).

`intfloat/multilingual-e5-small`: 384 dimensions, Arabic and English in one
space, small enough that the HNSW index stays resident on a free-tier host.

Imported lazily. The package is in the `[ml]` extra, and everything else in the
system works without it — installing the extra is what swaps the deterministic
development backend for this one, with no other change anywhere.

E5 models require prefixes: `query:` for the thing you are searching with,
`passage:` for the thing you are searching over. Omitting them costs real
retrieval quality and produces no error, which is the worst kind of mistake.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import structlog

from app.adapters.embeddings.base import Vector

log = structlog.get_logger(__name__)

DEFAULT_MODEL = "intfloat/multilingual-e5-small"


class SentenceTransformerEmbedder:
    def __init__(self, model_id: str = DEFAULT_MODEL, *, batch_size: int = 32) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - exercised by absence
            raise ImportError(
                "sentence-transformers is not installed. Install the ml extra: "
                'pip install -e ".[ml]"'
            ) from exc

        self.model_id = model_id
        self._batch_size = batch_size
        self._model: Any = SentenceTransformer(model_id)
        self.dim = int(self._model.get_sentence_embedding_dimension())
        log.info("embeddings.model_loaded", model=model_id, dim=self.dim)

    def encode(self, texts: Sequence[str], *, kind: str = "passage") -> list[Vector]:
        if not texts:
            return []
        prefixed = [f"{kind}: {text}" for text in texts]
        vectors = self._model.encode(
            prefixed,
            batch_size=self._batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return [[float(value) for value in vector] for vector in vectors]

    def encode_queries(self, texts: Sequence[str]) -> list[Vector]:
        return self.encode(texts, kind="query")


class CrossEncoderReranker:
    """`BAAI/bge-reranker-base` — joint scoring of a query-document pair."""

    def __init__(self, model_id: str = "BAAI/bge-reranker-base", *, batch_size: int = 16) -> None:
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                'sentence-transformers is not installed. Install: pip install -e ".[ml]"'
            ) from exc

        self.model_id = model_id
        self._batch_size = batch_size
        self._model: Any = CrossEncoder(model_id)
        log.info("rerank.model_loaded", model=model_id)

    def score_pairs(self, query: str, documents: Sequence[str]) -> list[float]:
        if not documents:
            return []
        pairs = [(query, document) for document in documents]
        scores = self._model.predict(pairs, batch_size=self._batch_size, show_progress_bar=False)
        return [float(score) for score in scores]
