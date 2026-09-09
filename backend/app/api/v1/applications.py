"""Engagement and application tracking (§12.4)."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, current_user, get_session
from app.services.engagement import (
    ApplicationStatus,
    DuplicateApplication,
    EngagementError,
    EngagementEvent,
    EngagementService,
)

router = APIRouter(tags=["engagement"])


class EventRequest(BaseModel):
    event: EngagementEvent


class ApplicationRequest(BaseModel):
    posting_id: uuid.UUID
    cv_version_id: uuid.UUID | None = None
    force: bool = False
    """Record the application anyway after a duplicate warning. The user's call."""


class StatusRequest(BaseModel):
    status: ApplicationStatus
    note: str | None = None


@router.post("/jobs/{posting_id}/events", status_code=status.HTTP_201_CREATED)
def record_event(
    posting_id: uuid.UUID,
    payload: EventRequest,
    user: Annotated[CurrentUser, Depends(current_user)],
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Record `viewed` / `saved` / `dismissed` / `applied` (§11.6 step 4)."""
    service = EngagementService(session)
    try:
        event_id = service.record_event(user.user_id, posting_id, payload.event)
    except EngagementError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"event_id": str(event_id), "event": payload.event.value}


@router.get("/jobs/saved")
def saved(
    user: Annotated[CurrentUser, Depends(current_user)],
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    rows = EngagementService(session).saved_postings(user.user_id)
    return {"saved": [_row(row) for row in rows], "count": len(rows)}


@router.post("/applications", status_code=status.HTTP_201_CREATED)
def create_application(
    payload: ApplicationRequest,
    user: Annotated[CurrentUser, Depends(current_user)],
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Record an application the user made themselves.

    CareerPilot never submits an application (§1.4); this records that the
    candidate went, so the tracker and the duplicate guard both work.
    """
    service = EngagementService(session)
    try:
        record = service.record_application(
            user.user_id,
            payload.posting_id,
            cv_version_id=payload.cv_version_id,
            force=payload.force,
        )
    except DuplicateApplication as exc:
        # 409 with the existing application, so a client can offer to open it
        # rather than silently refusing.
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(exc),
                "existing_application_id": str(exc.existing_id),
                "same_job_different_posting": exc.same_group,
                "hint": "Send force=true to record it anyway.",
            },
        ) from exc
    except EngagementError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return {
        "application_id": str(record.id),
        "posting_id": str(record.posting_id),
        "status": record.status.value,
        "cv_version_id": str(record.cv_version_id),
    }


@router.get("/applications")
def list_applications(
    user: Annotated[CurrentUser, Depends(current_user)],
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    service = EngagementService(session)
    rows = service.list_applications(user.user_id)
    return {
        "applications": [_row(row) for row in rows],
        "counters": service.counters(user.user_id),
    }


@router.patch("/applications/{application_id}")
def advance_application(
    application_id: uuid.UUID,
    payload: StatusRequest,
    user: Annotated[CurrentUser, Depends(current_user)],
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Advance status; every transition writes an event row (§11.6 step 7)."""
    try:
        record = EngagementService(session).advance(
            user.user_id, application_id, payload.status, note=payload.note
        )
    except EngagementError as exc:
        message = str(exc)
        raise HTTPException(
            status_code=404 if "not found" in message else 422, detail=message
        ) from exc
    return {"application_id": str(record.id), "status": record.status.value}


@router.get("/applications/{application_id}/history")
def application_history(
    application_id: uuid.UUID,
    user: Annotated[CurrentUser, Depends(current_user)],
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    history = EngagementService(session).application_history(user.user_id, application_id)
    if not history:
        raise HTTPException(status_code=404, detail="Application not found")
    return {"history": [_row(row) for row in history]}


def _row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: (str(value) if isinstance(value, uuid.UUID) else value) for key, value in row.items()
    }
