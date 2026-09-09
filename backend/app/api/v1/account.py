"""Account and compliance endpoints (§12.5)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, current_user, get_session
from app.config import get_settings
from app.logging import request_id_var
from app.services.account import AccountService
from app.services.auth import AuthService

router = APIRouter(prefix="/me", tags=["account"])

CONSENT_PURPOSES = ("processing", "digest", "analytics")


class ConsentRequest(BaseModel):
    purpose: Literal["processing", "digest", "analytics"]
    granted: bool


class DeleteRequest(BaseModel):
    confirm_email: str
    """Typing the address is the confirmation. Deletion is not undoable after
    the retention window, and a misplaced click should not trigger it."""


@router.get("/export")
def export_account(
    user: Annotated[CurrentUser, Depends(current_user)],
    session: Session = Depends(get_session),
) -> Response:
    """Everything held about the caller, as a downloadable JSON document.

    Job descriptions are third-party copyrighted text and are not republished
    (§16.3); titles and links are.
    """
    import json

    data = AccountService(session).export(user.user_id, request_id=request_id_var.get())
    body = json.dumps(data, indent=2, default=str)
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="careerpilot-export.json"'},
    )


@router.get("/consents")
def list_consents(
    user: Annotated[CurrentUser, Depends(current_user)],
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Consent history — every grant and every revocation, with its policy version."""
    rows = session.execute(
        text(
            "SELECT purpose, policy_version, granted_at, revoked_at FROM consents "
            "WHERE user_id = :id ORDER BY granted_at DESC"
        ),
        {"id": user.user_id},
    ).all()
    history = [dict(row._mapping) for row in rows]
    active = {row["purpose"] for row in history if row["revoked_at"] is None}
    return {
        "active": sorted(active),
        "available": list(CONSENT_PURPOSES),
        "history": history,
    }


@router.patch("/consents")
def update_consent(
    payload: ConsentRequest,
    user: Annotated[CurrentUser, Depends(current_user)],
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Grant or revoke by purpose. Revocation is honoured immediately (§11.7)."""
    service = AuthService(session, secret=get_settings().admin_token)
    account = AccountService(session)

    if payload.purpose == "processing" and not payload.granted:
        # Withdrawing the basis for processing is a deletion request in
        # substance; saying so is more honest than silently keeping the data.
        raise HTTPException(
            status_code=422,
            detail=(
                "Processing consent is what allows a CV to be handled at all. "
                "To withdraw it, delete your account: DELETE /api/v1/me"
            ),
        )

    if payload.granted:
        service.record_consent(user.user_id, payload.purpose)
    else:
        service.revoke_consent(user.user_id, payload.purpose)

    account.audit(
        user_id=user.user_id,
        actor=str(user.user_id),
        action="consent_granted" if payload.granted else "consent_revoked",
        subject=payload.purpose,
        request_id=request_id_var.get(),
    )
    return {"purpose": payload.purpose, "granted": payload.granted}


@router.delete("", status_code=status.HTTP_200_OK)
def delete_account(
    payload: DeleteRequest,
    user: Annotated[CurrentUser, Depends(current_user)],
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Close the account and destroy the CV text now; drop the rest after 30 days."""
    if payload.confirm_email.strip().lower() != user.email.strip().lower():
        raise HTTPException(status_code=422, detail="Type your email address to confirm deletion")

    receipt = AccountService(session).request_deletion(
        user.user_id, request_id=request_id_var.get()
    )
    return {
        "status": "deletion_requested",
        "cv_text_purged_immediately": receipt.cv_text_purged,
        "sessions_revoked": receipt.sessions_revoked,
        "remaining_data_deleted_after": receipt.hard_delete_after,
        "detail": (
            "Your CV text has been destroyed and every session ended. The "
            "remaining records are deleted after 30 days, which is the window "
            "in which an accidental deletion can still be reversed."
        ),
    }
