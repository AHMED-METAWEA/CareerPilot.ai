"""Embedding worker — document and sentence level (§7.2, §13 `embed`).

Two granularities, for two different jobs:

* **Document** vectors drive stage 3 retrieval. They are cheap and coarse, and
  they saturate — every backend engineering posting looks like every other one,
  which is fine for recall and useless for ranking.
* **Sentence** vectors, one per extracted requirement, drive requirement-level
  alignment in stage 5. They are what produces per-requirement evidence, and
  they discriminate far better than document similarity because they are not
  dominated by domain topicality.

Every row records the `model` that produced it. That is what makes an embedding
model change a dual-write migration rather than an index rebuild with a gap in
the middle (R7).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.adapters.embeddings.base import EmbeddingBackend, to_pgvector

log = structlog.get_logger(__name__)

MAX_DOCUMENT_CHARS = 6000
"""Beyond this a posting is mostly benefits boilerplate, which dilutes the vector."""


@dataclass(slots=True)
class EmbeddingReport:
    postings_embedded: int = 0
    requirements_embedded: int = 0
    model: str = ""


class EmbeddingService:
    def __init__(self, session: Session, embedder: EmbeddingBackend) -> None:
        self.session = session
        self.embedder = embedder

    def embed_pending(self, *, batch_size: int = 128) -> EmbeddingReport:
        """Embed postings and requirements that have no vector for this model."""
        report = EmbeddingReport(model=self.embedder.model_id)
        report.postings_embedded = self._embed_postings(batch_size)
        report.requirements_embedded = self._embed_requirements(batch_size)
        if report.postings_embedded or report.requirements_embedded:
            log.info(
                "embed.completed",
                postings=report.postings_embedded,
                requirements=report.requirements_embedded,
                model=report.model,
            )
        return report

    def _embed_postings(self, batch_size: int) -> int:
        rows = self.session.execute(
            text(
                """
                SELECT p.id, p.title, p.description_text
                  FROM job_postings p
                  LEFT JOIN job_embeddings e
                         ON e.posting_id = p.id AND e.model = :model
                 WHERE p.status = 'open' AND e.posting_id IS NULL
                 ORDER BY p.posted_at DESC NULLS LAST
                 LIMIT :limit
                """
            ),
            {"model": self.embedder.model_id, "limit": batch_size},
        ).all()
        if not rows:
            return 0

        texts = [f"{row.title}\n\n{row.description_text[:MAX_DOCUMENT_CHARS]}" for row in rows]
        vectors = self.embedder.encode(texts)

        for row, vector in zip(rows, vectors, strict=True):
            self.session.execute(
                text(
                    """
                    INSERT INTO job_embeddings (posting_id, model, dim, embedding)
                    VALUES (:posting_id, :model, :dim, :embedding)
                    ON CONFLICT (posting_id) DO UPDATE
                       SET model = EXCLUDED.model, dim = EXCLUDED.dim,
                           embedding = EXCLUDED.embedding, created_at = now()
                    """
                ),
                {
                    "posting_id": row.id,
                    "model": self.embedder.model_id,
                    "dim": self.embedder.dim,
                    "embedding": to_pgvector(vector),
                },
            )
        return len(rows)

    def _embed_requirements(self, batch_size: int) -> int:
        rows = self.session.execute(
            text(
                """
                SELECT id, text FROM job_requirements
                 WHERE embedding IS NULL OR model IS DISTINCT FROM :model
                 LIMIT :limit
                """
            ),
            {"model": self.embedder.model_id, "limit": batch_size},
        ).all()
        if not rows:
            return 0

        vectors = self.embedder.encode([row.text for row in rows])
        for row, vector in zip(rows, vectors, strict=True):
            self.session.execute(
                text(
                    "UPDATE job_requirements SET embedding = :embedding, model = :model "
                    "WHERE id = :id"
                ),
                {"id": row.id, "embedding": to_pgvector(vector), "model": self.embedder.model_id},
            )
        return len(rows)

    def embed_profile(self, profile_id: uuid.UUID) -> int:
        """(Re-)embed one profile's bullets and its document vector."""
        rows = self.session.execute(
            text("SELECT id, text FROM profile_bullets WHERE profile_id = :id ORDER BY ordinal"),
            {"id": profile_id},
        ).all()
        if not rows:
            return 0

        vectors = self.embedder.encode([row.text for row in rows])
        for row, vector in zip(rows, vectors, strict=True):
            self.session.execute(
                text(
                    "UPDATE profile_bullets SET embedding = :embedding, model = :model "
                    "WHERE id = :id"
                ),
                {"id": row.id, "embedding": to_pgvector(vector), "model": self.embedder.model_id},
            )

        document = self.embedder.encode([" ".join(row.text for row in rows)[:8000]])[0]
        self.session.execute(
            text(
                """
                INSERT INTO profile_embeddings (profile_id, model, dim, embedding)
                VALUES (:profile_id, :model, :dim, :embedding)
                ON CONFLICT (profile_id) DO UPDATE
                   SET model = EXCLUDED.model, embedding = EXCLUDED.embedding, created_at = now()
                """
            ),
            {
                "profile_id": profile_id,
                "model": self.embedder.model_id,
                "dim": self.embedder.dim,
                "embedding": to_pgvector(document),
            },
        )
        return len(rows)
