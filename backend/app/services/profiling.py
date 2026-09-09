"""Onboarding and profile construction — W1 (§11.1).

    upload → extract text → score parseability → (halt if unreadable)
           → redact PII → schema-constrained extraction → verify spans
           → resolve skills → persist → embed

Three properties this sequence exists to guarantee:

* **Nothing personal leaves the process unredacted.** Redaction happens before
  the prompt is built, and a post-redaction check runs before the call (§16.2).
* **Nothing unverifiable is stored.** Every field carries a quote, every quote
  is checked against the CV, and failures are discarded (§10.1).
* **An unreadable CV is reported, not guessed at.** Below the parse-quality
  floor the pipeline stops and returns the report (§11.1 step 5).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.adapters.documents import ExtractedDocument, extract
from app.adapters.embeddings.base import EmbeddingBackend, to_pgvector
from app.adapters.llm.base import ChatProvider, LLMError, complete_schema
from app.config import AppConfig
from app.domain.profile.bullets import Bullet, extract_bullets, verify_bullet_spans
from app.domain.profile.extraction import (
    EXTRACTION_SYSTEM_PROMPT,
    ExtractedProfile,
    GroundedProfile,
    build_extraction_prompt,
    ground_profile,
)
from app.domain.profile.parseability import MIN_QUALITY, ParseabilityReport, score_parseability
from app.domain.safety.redaction import assert_no_pii, redact
from app.services.taxonomy import load_taxonomy, record_unmapped

log = structlog.get_logger(__name__)

PARSER_VERSION = "cv-extract-1"


class CvUnreadableError(ValueError):
    """Parse quality below the floor. Carries the report the candidate is shown."""

    def __init__(self, report: ParseabilityReport) -> None:
        super().__init__(f"parse quality {report.quality} is below {MIN_QUALITY}")
        self.report = report


@dataclass(slots=True)
class IngestResult:
    cv_document_id: uuid.UUID
    cv_version_id: uuid.UUID
    version: int
    parseability: ParseabilityReport
    document: ExtractedDocument


@dataclass(slots=True)
class ProfileResult:
    profile_id: uuid.UUID
    grounded: GroundedProfile
    bullets: int
    skills_stored: int
    unmapped: int
    tokens_in: int = 0
    tokens_out: int = 0
    model: str | None = None


class ProfilingService:
    def __init__(
        self,
        session: Session,
        config: AppConfig,
        *,
        llm: ChatProvider | None = None,
        embedder: EmbeddingBackend | None = None,
    ) -> None:
        self.session = session
        self.config = config
        self.llm = llm
        self.embedder = embedder

    # ── W1 steps 2–5 ─────────────────────────────────────────────────

    def ingest_cv(
        self, user_id: uuid.UUID, data: bytes, mime: str, *, storage_key: str | None = None
    ) -> IngestResult:
        """Store the upload, extract its text, and score whether it is readable."""
        document = extract(data, mime)
        report = score_parseability(document.text, page_count=document.page_count)

        cv_document_id = self._upsert_document(user_id, document, storage_key)
        version, cv_version_id = self._insert_version(cv_document_id, document, report)

        log.info(
            "cv.ingested",
            user_id=str(user_id),
            cv_version_id=str(cv_version_id),
            quality=report.quality,
            pages=document.page_count,
            characters=len(document.text),
        )

        if not report.is_processable:
            # The row is kept: the candidate can see the report, fix the layout
            # and upload again, and we can measure how often this happens.
            raise CvUnreadableError(report)

        return IngestResult(
            cv_document_id=cv_document_id,
            cv_version_id=cv_version_id,
            version=version,
            parseability=report,
            document=document,
        )

    # ── W1 steps 6–12 ────────────────────────────────────────────────

    def build_profile(self, user_id: uuid.UUID, cv_version_id: uuid.UUID) -> ProfileResult:
        """Extract a structured profile from a stored CV version."""
        if self.llm is None:
            raise LLMError("no inference provider configured; set GROQ_API_KEY to build profiles")

        cv_text = self.session.execute(
            text("SELECT raw_text FROM cv_versions WHERE id = :id"), {"id": cv_version_id}
        ).scalar_one()

        # Step 6: redact before the text can reach a hosted model.
        redaction = redact(cv_text)
        leaked = assert_no_pii(redaction.text)
        if leaked:
            raise ValueError(f"redaction incomplete, refusing to send CV: {leaked}")

        # Step 7: schema-constrained extraction, temperature 0, one repair retry.
        extraction_model = self.config.models.extraction.model
        extracted, result = complete_schema(
            self.llm,
            system=EXTRACTION_SYSTEM_PROMPT,
            user=build_extraction_prompt(redaction.text),
            model=extraction_model,
            schema=ExtractedProfile,
        )

        # Steps 8–9: verify every span against the *original* text, resolve
        # skills against the closed vocabulary.
        taxonomy = load_taxonomy(
            self.session, fuzzy_threshold=self.config.scoring.skill_fuzzy_threshold
        )
        grounded = ground_profile(cv_text, extracted, taxonomy)

        profile_id = self._persist_profile(user_id, cv_version_id, grounded)
        skills_stored = self._persist_skills(profile_id, grounded)
        bullets = self._persist_bullets(profile_id, cv_text)
        unmapped = record_unmapped(
            self.session, [match.token for match in grounded.unmapped_skills]
        )
        self._record_model_run(result, grounded)

        log.info(
            "profile.built",
            profile_id=str(profile_id),
            skills=skills_stored,
            bullets=bullets,
            unmapped=unmapped,
            discarded=len(grounded.discarded),
            confidence=grounded.confidence,
        )
        return ProfileResult(
            profile_id=profile_id,
            grounded=grounded,
            bullets=bullets,
            skills_stored=skills_stored,
            unmapped=unmapped,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            model=result.model,
        )

    # ── persistence ──────────────────────────────────────────────────

    def _upsert_document(
        self, user_id: uuid.UUID, document: ExtractedDocument, storage_key: str | None
    ) -> uuid.UUID:
        """Same bytes from the same user are the same document (unique on sha256)."""
        row = self.session.execute(
            text(
                """
                INSERT INTO cv_documents (user_id, storage_key, mime, sha256)
                VALUES (:user_id, :storage_key, :mime, :sha256)
                ON CONFLICT (user_id, sha256) DO UPDATE SET uploaded_at = now()
                RETURNING id
                """
            ),
            {
                "user_id": user_id,
                "storage_key": storage_key or f"cv/{user_id}/{document.sha256}",
                "mime": document.mime,
                "sha256": document.sha256,
            },
        ).one()
        return uuid.UUID(str(row.id))

    def _insert_version(
        self,
        cv_document_id: uuid.UUID,
        document: ExtractedDocument,
        report: ParseabilityReport,
    ) -> tuple[int, uuid.UUID]:
        version = self.session.execute(
            text(
                "SELECT coalesce(max(version), 0) + 1 FROM cv_versions WHERE cv_document_id = :id"
            ),
            {"id": cv_document_id},
        ).scalar_one()
        row = self.session.execute(
            text(
                """
                INSERT INTO cv_versions (cv_document_id, version, raw_text, parse_quality,
                                         parser_version)
                VALUES (:cv_document_id, :version, :raw_text, :quality, :parser_version)
                RETURNING id
                """
            ),
            {
                "cv_document_id": cv_document_id,
                "version": version,
                "raw_text": document.text,
                "quality": report.quality,
                "parser_version": PARSER_VERSION,
            },
        ).one()
        return int(version), uuid.UUID(str(row.id))

    def _persist_profile(
        self, user_id: uuid.UUID, cv_version_id: uuid.UUID, grounded: GroundedProfile
    ) -> uuid.UUID:
        row = self.session.execute(
            text(
                """
                INSERT INTO candidate_profiles (
                    user_id, cv_version_id, years_experience, seniority_level,
                    locations, work_auth, languages, summary, extraction_confidence
                ) VALUES (
                    :user_id, :cv_version_id, :years, :seniority,
                    :locations, CAST(:work_auth AS jsonb), CAST(:languages AS jsonb),
                    :summary, :confidence
                )
                RETURNING id
                """
            ),
            {
                "user_id": user_id,
                "cv_version_id": cv_version_id,
                "years": grounded.years_experience,
                "seniority": grounded.seniority_level.value if grounded.seniority_level else None,
                "locations": [field.value for field in grounded.locations],
                "work_auth": json.dumps(
                    {
                        field.value.split(":")[0].strip(): field.value
                        for field in grounded.work_authorization
                    }
                ),
                "languages": json.dumps(
                    [{"lang": language, "cefr": cefr} for language, cefr in grounded.languages]
                ),
                "summary": grounded.summary or None,
                "confidence": grounded.confidence,
            },
        ).one()
        return uuid.UUID(str(row.id))

    def _persist_skills(self, profile_id: uuid.UUID, grounded: GroundedProfile) -> int:
        stored = 0
        for skill in grounded.skills:
            skill_id = self.session.execute(
                text("SELECT id FROM skills WHERE canonical_name = :name"),
                {"name": skill.canonical_name},
            ).scalar_one_or_none()
            if skill_id is None:
                continue  # resolved against a taxonomy row that has since gone
            span = skill.span.as_int4range() if skill.span else "[0,0)"
            self.session.execute(
                text(
                    """
                    INSERT INTO profile_skills (profile_id, skill_id, years, proficiency,
                                                evidence_span, source)
                    VALUES (:profile_id, :skill_id, :years, :proficiency,
                            CAST(:span AS int4range), 'extracted')
                    """
                ),
                {
                    "profile_id": profile_id,
                    "skill_id": skill_id,
                    "years": skill.years,
                    "proficiency": skill.proficiency,
                    "span": span,
                },
            )
            stored += 1
        return stored

    def _persist_bullets(self, profile_id: uuid.UUID, cv_text: str) -> int:
        bullets: list[Bullet] = verify_bullet_spans(cv_text, extract_bullets(cv_text))
        if not bullets:
            return 0

        vectors: list[list[float] | None] = [None] * len(bullets)
        model_id: str | None = None
        if self.embedder is not None:
            model_id = self.embedder.model_id
            vectors = list(self.embedder.encode([bullet.text for bullet in bullets]))

        for bullet, vector in zip(bullets, vectors, strict=True):
            self.session.execute(
                text(
                    """
                    INSERT INTO profile_bullets (profile_id, ordinal, text, span, section,
                                                 model, embedding)
                    VALUES (:profile_id, :ordinal, :text, CAST(:span AS int4range), :section,
                            :model, :embedding)
                    """
                ),
                {
                    "profile_id": profile_id,
                    "ordinal": bullet.ordinal,
                    "text": bullet.text,
                    "span": bullet.span.as_int4range(),
                    "section": bullet.section,
                    "model": model_id,
                    "embedding": to_pgvector(vector) if vector is not None else None,
                },
            )

        if self.embedder is not None:
            document_vector = self.embedder.encode(
                [" ".join(bullet.text for bullet in bullets)[:8000]]
            )[0]
            self.session.execute(
                text(
                    """
                    INSERT INTO profile_embeddings (profile_id, model, dim, embedding)
                    VALUES (:profile_id, :model, :dim, :embedding)
                    ON CONFLICT (profile_id) DO UPDATE
                       SET model = EXCLUDED.model, embedding = EXCLUDED.embedding,
                           created_at = now()
                    """
                ),
                {
                    "profile_id": profile_id,
                    "model": self.embedder.model_id,
                    "dim": self.embedder.dim,
                    "embedding": to_pgvector(document_vector),
                },
            )
        return len(bullets)

    def _record_model_run(self, result: Any, grounded: GroundedProfile) -> None:
        """Every LLM call records what it cost and whether it validated (§17.2)."""
        self.session.execute(
            text(
                """
                INSERT INTO model_runs (component, model, version, params, metrics)
                VALUES ('profile_extraction', :model, :version,
                        CAST(:params AS jsonb), CAST(:metrics AS jsonb))
                """
            ),
            {
                "model": result.model,
                "version": PARSER_VERSION,
                "params": json.dumps({"provider": result.provider, "attempts": result.attempts}),
                "metrics": json.dumps(
                    {
                        "tokens_in": result.tokens_in,
                        "tokens_out": result.tokens_out,
                        "latency_ms": round(result.latency_ms, 1),
                        "repaired": result.repaired,
                        "fields_checked": grounded.fields_checked,
                        "fields_discarded": len(grounded.discarded),
                        "hallucination_rate": round(grounded.hallucination_rate, 4),
                        "unmapped_skills": len(grounded.unmapped_skills),
                    }
                ),
            },
        )
