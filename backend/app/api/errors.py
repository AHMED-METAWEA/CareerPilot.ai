"""RFC 7807 problem responses (§12.7).

Every error the API emits is `application/problem+json` with a stable `type`
URI, and every response carries the request id so a user report can be traced
straight to a log line.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.logging import request_id_var

log = structlog.get_logger(__name__)

PROBLEM_BASE = "https://careerpilot.ai/problems"


def problem(
    status: int,
    title: str,
    *,
    detail: str | None = None,
    type_: str = "about:blank",
    **extra: Any,
) -> JSONResponse:
    body: dict[str, Any] = {"type": type_, "title": title, "status": status}
    if detail:
        body["detail"] = detail
    request_id = request_id_var.get()
    if request_id:
        body["request_id"] = request_id
    body.update(extra)
    return JSONResponse(
        status_code=status,
        content=body,
        media_type="application/problem+json",
        headers={"X-Request-ID": request_id} if request_id else None,
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def http_exception(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return problem(
            exc.status_code,
            title=str(exc.detail),
            type_=f"{PROBLEM_BASE}/http-{exc.status_code}",
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return problem(
            422,
            title="Request validation failed",
            type_=f"{PROBLEM_BASE}/validation-error",
            errors=[
                {"loc": list(err.get("loc", ())), "msg": err.get("msg"), "type": err.get("type")}
                for err in exc.errors()
            ],
        )

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Log the detail; return none of it. An internal message is an
        # information leak, and the request id is enough to find the trace.
        log.exception(
            "api.unhandled_exception",
            path=request.url.path,
            error=f"{type(exc).__name__}: {exc}",
        )
        return problem(
            500,
            title="Internal server error",
            detail="The request could not be completed. Quote the request id when reporting this.",
            type_=f"{PROBLEM_BASE}/internal-error",
        )
