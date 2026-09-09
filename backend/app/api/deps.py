"""Shared request dependencies."""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
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


def current_user_id(
    x_user_id: Annotated[uuid.UUID | None, Header()] = None,
    session: Session = Depends(get_session),
) -> uuid.UUID:
    """Resolve the acting user.

    Phase 1 has no authentication: the plan puts multi-user auth in Phase 3
    (§18), and inventing a half-authentication now would be worse than having
    none — it would look like a security boundary without being one. The header
    is validated against the users table so a request cannot act as a user that
    does not exist, and `require_real_auth` refuses to serve this path in
    production at all.
    """
    settings = get_settings()
    if settings.careerpilot_env == "production":
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Authentication arrives in Phase 3; these endpoints are not production-ready",
        )
    if x_user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-User-Id header is required until authentication ships (Phase 3)",
        )
    exists = session.execute(
        text("SELECT 1 FROM users WHERE id = :id AND deleted_at IS NULL"), {"id": x_user_id}
    ).first()
    if not exists:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown user")
    return x_user_id


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
