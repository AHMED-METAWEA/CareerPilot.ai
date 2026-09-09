"""FastAPI application (§12).

Phase 0 exposes health and the admin surface. The candidate, matching and
engagement routers arrive with Phases 1 and 3; their absence here is scope
control, not an omission.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, Request, Response
from sqlalchemy import text

from app.api.errors import install_error_handlers
from app.api.v1 import admin
from app.config import get_settings
from app.db.session import get_engine
from app.logging import configure_logging, request_id_var

log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level, json_output=settings.careerpilot_env != "local")
    log.info("api.startup", environment=settings.careerpilot_env)
    yield
    get_engine().dispose()
    log.info("api.shutdown")


app = FastAPI(
    title="CareerPilot.ai API",
    version="0.1.0",
    summary="AI-assisted job discovery and candidate–role matching, built on verifiable data.",
    lifespan=lifespan,
)

install_error_handlers(app)
app.include_router(admin.router, prefix="/api/v1")


@app.middleware("http")
async def request_id_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Propagate `X-Request-ID` through logs and into worker tasks (§12.7, §17.2)."""
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    token = request_id_var.set(request_id)
    try:
        response = await call_next(request)
    finally:
        request_id_var.reset(token)
    response.headers["X-Request-ID"] = request_id
    return response


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    """Liveness: the process is up. Deliberately does not touch the database."""
    return {"status": "ok", "version": app.version}


@app.get("/health/ready", tags=["health"])
def readiness() -> dict[str, Any]:
    """Readiness: the database answers and the schema is at head."""
    with get_engine().connect() as connection:
        connection.execute(text("SELECT 1"))
        revision = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one_or_none()
    return {"status": "ok", "schema_revision": revision}
