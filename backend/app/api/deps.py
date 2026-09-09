"""Shared request dependencies."""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.session import get_sessionmaker


def get_session() -> Iterator[Session]:
    """One transaction per request, committed on success."""
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@dataclass(frozen=True, slots=True)
class CurrentUser:
    user_id: uuid.UUID
    email: str


_bearer = HTTPBearer(auto_error=False)


def current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    session: Session = Depends(get_session),
) -> CurrentUser:
    """The authenticated caller (§12.1, §16.2).

    Every subsequent query is scoped by this id at the repository layer rather
    than the route layer (§16.2), so a handler that forgets to filter cannot
    leak another user's data.
    """
    from app.services.auth import AuthError, AuthService

    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization: Bearer <access token> is required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    service = AuthService(session, secret=get_settings().admin_token)
    try:
        user = service.verify_access_token(credentials.credentials)
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    # A token outlives a deletion request by up to fifteen minutes; the account
    # check closes that window.
    active = session.execute(
        text("SELECT 1 FROM users WHERE id = :id AND deleted_at IS NULL"),
        {"id": user.user_id},
    ).first()
    if not active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account not active")

    return CurrentUser(user_id=user.user_id, email=user.email)


def current_user_id(user: Annotated[CurrentUser, Depends(current_user)]) -> uuid.UUID:
    """Convenience for handlers that only need the id."""
    return user.user_id


def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    """Shared-secret guard for the admin surface.

    Phase 0 has no user accounts; JWT auth arrives with the multi-user product
    in Phase 3 and replaces this. The comparison is constant-time, and a
    production deployment that has not changed the default token is refused
    rather than quietly protected by a published string.
    """
    settings = get_settings()
    if (
        settings.careerpilot_env == "production"
        and settings.admin_token == "change-me-in-production"
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ADMIN_TOKEN is unset in production",
        )
    if not x_admin_token or not secrets.compare_digest(x_admin_token, settings.admin_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing admin token"
        )
