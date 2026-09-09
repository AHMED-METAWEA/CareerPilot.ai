"""Candidate tooling — gaps, interview prep, cover letters, bullets (§12.4).

Two of these four endpoints work with no inference provider configured, and two
do not. That split is deliberate and visible in the responses: gap analysis and
interview preparation are derived from data the system already holds, so they
are always available and always the same. Writing needs a model, and when one is
not reachable the endpoint says so plainly rather than degrading into something
generic.

Every generated response carries `checked: true` — the anti-invention diff ran
and passed (§10.3). A response that could not pass it is a 422, not a
best-effort draft with a warning attached.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Body, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.adapters.http import HttpClient
from app.adapters.llm import build_provider
from app.adapters.llm.base import ChatProvider, LLMError
from app.api.deps import current_user_id, get_session
from app.config import AppConfig, get_config, get_settings
from app.services.tooling import GenerationRefused, ToolingError, ToolingService

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/matches", tags=["tooling"])


def chat_provider() -> ChatProvider | None:
    """The provider used for generation.

    A dependency rather than a call inside the handler so that a test can supply
    a stub. A suite whose result depends on whether an API key happens to be in
    the environment is a suite that fails on someone else's schedule.
    """
    config = get_config()
    http = HttpClient(
        user_agent=config.ingestion.user_agent,
        timeout_seconds=config.ingestion.request_timeout_seconds,
    )
    try:
        return build_provider(get_settings(), http, config.models.fallbacks)
    except LLMError as exc:
        # Not fatal: the service raises a ToolingError worded for the candidate,
        # which is better than a stack trace about a missing key.
        log.warning("tooling.no_provider", error=str(exc))
        return None


def _service(
    session: Session, config: AppConfig, llm: ChatProvider | None = None
) -> ToolingService:
    return ToolingService(session, config, llm=llm)


@router.get("/{match_id}/gaps")
def gaps(
    match_id: uuid.UUID,
    user_id: uuid.UUID = Depends(current_user_id),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """What this posting asks for that the CV does not evidence (§18, Phase 4).

    Deterministic: the same gaps the scorer used, reported the same way twice.
    """
    service = _service(session, get_config())
    try:
        analysis = service.gap_analysis(user_id, match_id)
    except ToolingError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return {
        "matched": analysis.matched,
        "gaps": [
            {
                "skill": gap.skill,
                "must_have": gap.is_must_have,
                "suggestion": gap.suggestion,
            }
            for gap in analysis.gaps
        ],
        "blocking_count": len(analysis.blocking),
        "note": analysis.note,
    }


@router.get("/{match_id}/interview")
def interview(
    match_id: uuid.UUID,
    user_id: uuid.UUID = Depends(current_user_id),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Preparation derived from the posting's own extracted requirements."""
    service = _service(session, get_config())
    try:
        return service.interview_preparation(user_id, match_id)
    except ToolingError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{match_id}/bullets")
def bullets(
    match_id: uuid.UUID,
    user_id: uuid.UUID = Depends(current_user_id),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """The candidate's own CV bullets — the only text this API will rewrite."""
    service = _service(session, get_config())
    try:
        return {"bullets": service.own_bullets(user_id, match_id)}
    except ToolingError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{match_id}/cover-letter", status_code=status.HTTP_201_CREATED)
def cover_letter(
    match_id: uuid.UUID,
    user_id: uuid.UUID = Depends(current_user_id),
    session: Session = Depends(get_session),
    llm: ChatProvider | None = Depends(chat_provider),
) -> dict[str, Any]:
    """A draft assembled from facts and verified against the CV (§10.4)."""
    service = _service(session, get_config(), llm)
    try:
        result = service.cover_letter(user_id, match_id)
    except GenerationRefused as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": (
                    "The draft claimed things your CV does not support, so it was not shown "
                    "to you. This is the guard working, not a failure on your part."
                ),
                "invented": [str(item) for item in exc.result.invented],
            },
        ) from exc
    except ToolingError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return {
        "letter": result["letter"],
        "checked": True,
        "disclosure": (
            "Drafted by a language model from your CV and this posting, then checked "
            "line by line against your CV. Read it before you send it — it is your name "
            "on it, not ours."
        ),
    }


@router.post("/{match_id}/bullet", status_code=status.HTTP_200_OK)
def rewrite_bullet(
    match_id: uuid.UUID,
    bullet: str = Body(..., embed=True, min_length=10, max_length=1000),
    user_id: uuid.UUID = Depends(current_user_id),
    session: Session = Depends(get_session),
    llm: ChatProvider | None = Depends(chat_provider),
) -> dict[str, Any]:
    """Rephrase one of the candidate's own bullets toward this posting.

    The bullet must be one of theirs. Rewriting arbitrary submitted text would
    turn this into a laundering route for a claim the CV never made.
    """
    service = _service(session, get_config(), llm)
    try:
        result = service.rewrite_bullet(user_id, match_id, bullet)
    except GenerationRefused as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "The rewrite added something your CV does not say, so it was discarded.",
                "invented": [str(item) for item in exc.result.invented],
            },
        ) from exc
    except ToolingError as exc:
        status_code = 404 if "not from your CV" in str(exc) else 503
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc

    return {"original": result["original"], "rewritten": result["rewritten"], "checked": True}
