"""Shared request dependencies."""

from __future__ import annotations

import secrets
from collections.abc import Iterator

from fastapi import Header, HTTPException, status
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
