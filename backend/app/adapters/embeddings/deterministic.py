"""Development embedding backend — no model weights, no downloads.

**This is not a semantic model.** It is a hashed character-n-gram projection:
it measures lexical overlap, deterministically and in microseconds. Documents
sharing words score high; "NLP" and "معالجة اللغة الطبيعية" score zero, which
is exactly what the real multilingual model exists to fix.

It is here so that the whole pipeline — retrieval, alignment, evidence, the API
— can be built, tested and demonstrated before committing 2.5 GB of weights to
a laptop, and so CI never downloads a model. `model_id` says plainly what it is,
and it is written to `job_embeddings.model`, so vectors produced by it can never
be confused with the real ones (R7).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from itertools import pairwise

from app.adapters.embeddings.base import Vector, normalize

_TOKEN = re.compile(r"[\w؀-ۿ+#.]+")

DIM = 384  # matches multilingual-e5-small, so the schema does not change on swap
MODEL_ID = "deterministic-hash-v1"


class DeterministicEmbedder:
    """Hashed lexical projection. Same input, same vector, always."""

    dim = DIM
    model_id = MODEL_ID

    def __init__(self, dim: int = DIM) -> None:
        self.dim = dim

    def encode(self, texts: Sequence[str]) -> list[Vector]:
        return [self._encode_one(text) for text in texts]

    def _encode_one(self, text: str) -> Vector:
        vector = [0.0] * self.dim
        tokens = _TOKEN.findall(text.casefold())
        if not tokens:
            return vector

        for token in tokens:
            self._add(vector, token, weight=1.0)
            # Character trigrams give partial credit for morphology and typos,
            # which keeps the development pipeline from looking absurdly brittle.
            padded = f"^{token}$"
            for index in range(len(padded) - 2):
                self._add(vector, padded[index : index + 3], weight=0.35)

        for bigram in pairwise(tokens):
            self._add(vector, " ".join(bigram), weight=0.6)

        return normalize(vector)

    def _add(self, vector: Vector, feature: str, *, weight: float) -> None:
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        position = int.from_bytes(digest[:4], "big") % self.dim
        # Signed hashing: without it every feature pushes in one direction and
        # unrelated documents drift toward similarity.
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[position] += sign * weight


class DeterministicReranker:
    """Stand-in cross-encoder: token overlap weighted toward rarer terms."""

    model_id = "deterministic-rerank-v1"

    def score_pairs(self, query: str, documents: Sequence[str]) -> list[float]:
        query_tokens = set(_TOKEN.findall(query.casefold()))
        if not query_tokens:
            return [0.0] * len(documents)

        scores: list[float] = []
        for document in documents:
            document_tokens = set(_TOKEN.findall(document.casefold()))
            if not document_tokens:
                scores.append(0.0)
                continue
            overlap = query_tokens & document_tokens
            # Jaccard, tilted toward covering the query rather than the document,
            # because a long job description should not be penalised for length.
            coverage = len(overlap) / len(query_tokens)
            jaccard = len(overlap) / len(query_tokens | document_tokens)
            scores.append(round(0.7 * coverage + 0.3 * jaccard, 6))
        return scores
