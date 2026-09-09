"""Authentication endpoints (§12.1)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, current_user, get_session
from app.config import get_settings
from app.domain.identity import MIN_PASSWORD_LENGTH
from app.services.auth import AuthError, AuthService, InvalidCredentials, WeakPassword

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=MIN_PASSWORD_LENGTH)
    locale: str = "en"
    accept_processing: bool = Field(
        default=False,
        description=(
            "Consent to processing the CV for matching. Required: the system "
            "cannot lawfully process a CV without it (§16.3)."
        ),
    )
    accept_digest: bool = False


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


def _service(session: Session) -> AuthService:
    return AuthService(session, secret=get_settings().admin_token)


@router.post("/register", status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, session: Session = Depends(get_session)) -> dict[str, Any]:
    """Create an account, recording consent and its policy version (§11.1)."""
    if not payload.accept_processing:
        raise HTTPException(
            status_code=422,
            detail=(
                "Consent to processing is required before a CV can be handled. "
                "Nothing is processed without it."
            ),
        )

    consents = ["processing"] + (["digest"] if payload.accept_digest else [])
    service = _service(session)
    try:
        user = service.register(
            str(payload.email),
            payload.password,
            locale=payload.locale,
            consents=tuple(consents),
        )
    except WeakPassword as exc:
        raise HTTPException(
            status_code=422, detail={"message": str(exc), "problems": exc.problems}
        ) from exc
    except AuthError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    tokens = service.issue_tokens(user)
    return {"user_id": str(user.user_id), "email": user.email, **tokens.as_dict()}


@router.post("/login")
def login(payload: LoginRequest, session: Session = Depends(get_session)) -> dict[str, Any]:
    service = _service(session)
    try:
        user = service.authenticate(str(payload.email), payload.password)
    except InvalidCredentials as exc:
        # Deliberately identical for an unknown email and a wrong password.
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    tokens = service.issue_tokens(user)
    return {"user_id": str(user.user_id), "email": user.email, **tokens.as_dict()}


@router.post("/refresh")
def refresh(payload: RefreshRequest, session: Session = Depends(get_session)) -> dict[str, Any]:
    """Exchange a refresh token for a new pair. Single use (§16.2)."""
    try:
        tokens = _service(session).rotate(payload.refresh_token)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return tokens.as_dict()


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    payload: RefreshRequest,
    user: Annotated[CurrentUser, Depends(current_user)],
    session: Session = Depends(get_session),
) -> None:
    """Revoke one refresh token. The access token expires on its own."""
    _service(session).revoke_refresh_token(payload.refresh_token)


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
def logout_all(
    user: Annotated[CurrentUser, Depends(current_user)],
    session: Session = Depends(get_session),
) -> None:
    """Revoke every session. What a user reaches for after losing a laptop."""
    _service(session).revoke_all(user.user_id)
