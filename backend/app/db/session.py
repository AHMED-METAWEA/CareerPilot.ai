"""Engine and session management.

One engine per process. Sessions are short-lived and always closed; every
worker task and every request runs inside exactly one transaction boundary so
that a failure rolls the whole unit of work back (§13.2 idempotency).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Any, cast

from sqlalchemy import CursorResult, Engine, Result, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    settings = get_settings()
    engine = create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=5,
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _set_statement_timeout(dbapi_connection: object, _: object) -> None:
        # A runaway query must not hold a worker slot for the whole night.
        with dbapi_connection.cursor() as cursor:  # type: ignore[attr-defined]
            cursor.execute("SET statement_timeout = '120s'")

    return engine


@lru_cache(maxsize=1)
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on any exception."""
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine_cache() -> None:
    """Test hook: drop the cached engine so a new DATABASE_URL takes effect."""
    get_sessionmaker.cache_clear()
    if get_engine.cache_info().currsize:
        get_engine().dispose()
    get_engine.cache_clear()


def rows_affected(result: Result[Any]) -> int:
    """Rows touched by a DML statement.

    `Session.execute` is typed as returning `Result`, which has no `rowcount`;
    every DML statement actually returns a `CursorResult`. The cast is narrowed
    here once rather than repeated at each call site.
    """
    return int(cast("CursorResult[Any]", result).rowcount or 0)
