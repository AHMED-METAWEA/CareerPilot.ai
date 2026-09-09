"""CV upload and candidate profile (§12.2)."""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.adapters.documents import (
    SUPPORTED_MIMES,
    DocumentTooLargeError,
    ExtractionFailedError,
    UnsupportedDocumentError,
)
from app.adapters.embeddings import build_embedder
from app.adapters.http import HttpClient
from app.adapters.llm import build_provider
from app.adapters.llm.base import LLMError
from app.api.deps import current_user_id, get_session
from app.config import AppConfig, get_config, get_settings
from app.services.profiling import CvUnreadableError, ProfilingService

log = structlog.get_logger(__name__)
router = APIRouter(tags=["profile"])


def _service(session: Session, config: AppConfig) -> ProfilingService:
    settings = get_settings()
    http = HttpClient(
        user_agent=config.ingestion.user_agent,
        timeout_seconds=config.ingestion.request_timeout_seconds,
    )
    try:
        llm = build_provider(settings, http, config.models.fallbacks)
    except LLMError:
        llm = None
    return ProfilingService(
        session, config, llm=llm, embedder=build_embedder(config.models.embedding)
    )


@router.post("/cv", status_code=status.HTTP_201_CREATED)
async def upload_cv(
    file: UploadFile = File(...),
    user_id: uuid.UUID = Depends(current_user_id),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Upload a CV. Returns the version id and its parse quality (§11.1)."""
    config = get_config()
    data = await file.read()
    mime = file.content_type or "application/octet-stream"
    if mime not in SUPPORTED_MIMES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Supported types: {', '.join(sorted(SUPPORTED_MIMES))}",
        )

    service = _service(session, config)
    try:
        result = service.ingest_cv(user_id, data, mime)
    except DocumentTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except (UnsupportedDocumentError, ExtractionFailedError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except CvUnreadableError as exc:
        # 422 with the report, not a bare rejection: the candidate can act on it.
        raise HTTPException(
            status_code=422,
            detail={
                "message": "This CV could not be read reliably enough to extract from.",
                "parseability": _report(exc.report),
            },
        ) from exc

    return {
        "cv_document_id": str(result.cv_document_id),
        "cv_version_id": str(result.cv_version_id),
        "version": result.version,
        "parse_quality": result.parseability.quality,
        "parseability": _report(result.parseability),
    }


@router.post("/cv/{cv_version_id}/profile", status_code=status.HTTP_201_CREATED)
def build_profile(
    cv_version_id: uuid.UUID,
    user_id: uuid.UUID = Depends(current_user_id),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Extract a structured profile from an uploaded CV version."""
    owns = session.execute(
        text(
            """
            SELECT 1 FROM cv_versions v
              JOIN cv_documents d ON d.id = v.cv_document_id
             WHERE v.id = :id AND d.user_id = :user_id
            """
        ),
        {"id": cv_version_id, "user_id": user_id},
    ).first()
    if not owns:
        raise HTTPException(status_code=404, detail="CV version not found")

    try:
        result = _service(session, get_config()).build_profile(user_id, cv_version_id)
    except LLMError as exc:
        # The candidate is not the right audience for a provider's connection
        # error. Their CV is stored and readable; what failed is ours to fix.
        log.error("profile.extraction_unavailable", error=str(exc))
        raise HTTPException(
            status_code=503,
            detail=(
                "Your CV was stored and read successfully, but profile extraction is "
                "temporarily unavailable. Nothing was lost — try again shortly."
            ),
        ) from exc

    return {
        "profile_id": str(result.profile_id),
        "skills": [skill.canonical_name for skill in result.grounded.skills],
        "bullets": result.bullets,
        "unmapped_skills": [match.token for match in result.grounded.unmapped_skills],
        "discarded_fields": result.grounded.discarded,
        "extraction_confidence": result.grounded.confidence,
        "model": result.model,
    }


@router.get("/cv/{cv_version_id}/parseability")
def parseability(
    cv_version_id: uuid.UUID,
    user_id: uuid.UUID = Depends(current_user_id),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """The stored parse-quality report for a CV version."""
    row = session.execute(
        text(
            """
            SELECT v.parse_quality, v.raw_text, v.parser_version, v.created_at
              FROM cv_versions v
              JOIN cv_documents d ON d.id = v.cv_document_id
             WHERE v.id = :id AND d.user_id = :user_id
            """
        ),
        {"id": cv_version_id, "user_id": user_id},
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="CV version not found")

    from app.domain.profile.parseability import score_parseability

    report = score_parseability(row.raw_text)
    return {
        "cv_version_id": str(cv_version_id),
        "stored_quality": float(row.parse_quality),
        "parser_version": row.parser_version,
        "parseability": _report(report),
    }


@router.get("/profile")
def get_profile(
    user_id: uuid.UUID = Depends(current_user_id),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Current profile, with per-field evidence spans (§12.2)."""
    row = session.execute(
        text(
            """
            SELECT id, cv_version_id, years_experience, seniority_level, locations,
                   work_auth, languages, summary, extraction_confidence, extracted_at
              FROM candidate_profiles
             WHERE user_id = :user_id
             ORDER BY extracted_at DESC LIMIT 1
            """
        ),
        {"user_id": user_id},
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="No profile yet; upload a CV first")

    skills = session.execute(
        text(
            """
            SELECT s.canonical_name, ps.years, ps.proficiency, ps.source,
                   lower(ps.evidence_span) AS span_start, upper(ps.evidence_span) AS span_end
              FROM profile_skills ps
              JOIN skills s ON s.id = ps.skill_id
             WHERE ps.profile_id = :id
             ORDER BY s.canonical_name
            """
        ),
        {"id": row.id},
    ).all()

    return {
        "profile_id": str(row.id),
        "cv_version_id": str(row.cv_version_id),
        "years_experience": float(row.years_experience) if row.years_experience else None,
        "seniority_level": row.seniority_level,
        "locations": list(row.locations or []),
        "work_authorization": row.work_auth or {},
        "languages": row.languages or [],
        "summary": row.summary,
        "extraction_confidence": float(row.extraction_confidence)
        if row.extraction_confidence
        else None,
        "skills": [
            {
                "name": skill.canonical_name,
                "years": float(skill.years) if skill.years else None,
                "proficiency": skill.proficiency,
                "source": skill.source,
                # Every claim points at the words in the CV that justify it.
                "evidence_span": [skill.span_start, skill.span_end],
            }
            for skill in skills
        ],
        "extracted_at": row.extracted_at,
    }


def _report(report: Any) -> dict[str, Any]:
    return {
        "quality": report.quality,
        "is_processable": report.is_processable,
        "character_yield_per_page": report.character_yield,
        "sections_found": list(report.sections_found),
        "sections_missing": list(report.sections_missing),
        "layout_damage": report.layout_damage,
        "page_count": report.page_count,
        "findings": [
            {
                "code": finding.code,
                "severity": finding.severity.value,
                "message": finding.message,
                "suggestion": finding.suggestion,
            }
            for finding in report.findings
        ],
    }
