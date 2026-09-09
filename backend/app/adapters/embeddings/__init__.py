"""Embedding and reranking backends.

The domain talks to protocols; these implement them. `DeterministicEmbedder`
lets the whole pipeline run with no model weights installed, and
`SentenceTransformerEmbedder` swaps in when the `[ml]` extra is present —
without either the domain or the services changing.
"""

from app.adapters.embeddings.base import (
    EmbeddingBackend,
    cosine,
    normalize,
)
from app.adapters.embeddings.deterministic import (
    DeterministicEmbedder,
    DeterministicReranker,
)
from app.adapters.embeddings.factory import build_embedder, build_reranker

__all__ = [
    "DeterministicEmbedder",
    "DeterministicReranker",
    "EmbeddingBackend",
    "build_embedder",
    "build_reranker",
    "cosine",
    "normalize",
    "parse_vector",
    "to_pgvector",
]
