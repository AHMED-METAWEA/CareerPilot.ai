"""Task handler registry.

A handler is a callable taking a `TaskContext` and the task payload. Handlers
run inside one transaction opened by the runner, so a handler that raises
leaves no partial state behind (§13.2 idempotency).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.adapters.http import HttpClient
from app.config import AppConfig


@dataclass(frozen=True, slots=True)
class TaskContext:
    session: Session
    http: HttpClient
    config: AppConfig


Handler = Callable[[TaskContext, dict[str, Any]], dict[str, Any]]

HANDLERS: dict[str, Handler] = {}


def handler(task_type: str) -> Callable[[Handler], Handler]:
    def decorate(fn: Handler) -> Handler:
        HANDLERS[task_type] = fn
        return fn

    return decorate
