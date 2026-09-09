"""Durable task queue on Postgres (§13.1).

Lives in the persistence layer rather than under `app/workers/` because that is
what it is: one table and the SQL that manipulates it. Services enqueue work and
the admin API triggers runs, and neither may import the worker layer under the
§14.1 import contract — the queue is what they share, not the runner.

`SELECT … FOR UPDATE SKIP LOCKED` gives a transactional, observable queue with
no extra infrastructure: a claim is visible in ordinary SQL, a crash releases
the row when the transaction dies, and queue state is consistent with domain
state because it is the same database. Redis arrives when pub/sub or a genuine
cache need does — not before (ADR 0005).
"""

from __future__ import annotations

import os
import socket
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.session import rows_affected

log = structlog.get_logger(__name__)

WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"


class TaskType:
    DISCOVER = "discover"
    FETCH_DETAILS = "fetch_details"
    RESOLVE_ENTITIES = "resolve_entities"
    DEDUPLICATE = "deduplicate"
    EMBED = "embed"
    VERIFY_URLS = "verify_urls"
    MATCH_USERS = "match_users"
    DIGEST = "digest"
    EXPIRE_POSTINGS = "expire_postings"
    PRUNE_RAW = "prune_raw"
    RUN_EVAL = "run_eval"
    PURGE_DELETED = "purge_deleted"


@dataclass(frozen=True, slots=True)
class Task:
    id: uuid.UUID
    task_type: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int


def enqueue(
    session: Session,
    task_type: str,
    payload: dict[str, Any] | None = None,
    *,
    priority: int = 0,
    run_after: datetime | None = None,
    dedup_key: str | None = None,
    max_attempts: int = 4,
) -> uuid.UUID | None:
    """Enqueue a task, or return None if an identical one is already waiting.

    `dedup_key` makes enqueueing idempotent: the hourly scheduler can fire twice
    and the source is still discovered once (§13.2).
    """
    row = session.execute(
        text(
            """
            INSERT INTO task_queue (task_type, payload, priority, run_after, dedup_key, max_attempts)
            VALUES (:task_type, CAST(:payload AS jsonb), :priority,
                    COALESCE(:run_after, now()), :dedup_key, :max_attempts)
            ON CONFLICT DO NOTHING
            RETURNING id
            """
        ),
        {
            "task_type": task_type,
            "payload": _json(payload or {}),
            "priority": priority,
            "run_after": run_after,
            "dedup_key": dedup_key,
            "max_attempts": max_attempts,
        },
    ).first()
    return row[0] if row else None


def claim(
    session: Session, *, worker_id: str = WORKER_ID, task_types: list[str] | None = None
) -> Task | None:
    """Claim one runnable task. Concurrent workers never claim the same row."""
    filter_clause = "AND task_type = ANY(:task_types)" if task_types else ""
    row = session.execute(
        text(
            f"""
            UPDATE task_queue
               SET status = 'running',
                   locked_at = now(),
                   locked_by = :worker_id,
                   attempts = attempts + 1,
                   updated_at = now()
             WHERE id = (
               SELECT id FROM task_queue
                WHERE status = 'pending' AND run_after <= now() {filter_clause}
                ORDER BY priority DESC, run_after
                FOR UPDATE SKIP LOCKED
                LIMIT 1
             )
            RETURNING id, task_type, payload, attempts, max_attempts
            """
        ),
        {"worker_id": worker_id, "task_types": task_types},
    ).first()
    if row is None:
        return None
    return Task(
        id=row.id,
        task_type=row.task_type,
        payload=row.payload or {},
        attempts=row.attempts,
        max_attempts=row.max_attempts,
    )


def complete(session: Session, task_id: uuid.UUID) -> None:
    session.execute(
        text(
            """
            UPDATE task_queue
               SET status = 'done', locked_at = NULL, locked_by = NULL,
                   last_error = NULL, updated_at = now()
             WHERE id = :id
            """
        ),
        {"id": task_id},
    )


def backoff_delay(attempts: int, schedule: list[int]) -> timedelta | None:
    """1 min, 5 min, 25 min, then dead-letter (§13.2).

    `attempts` is the count *including* the attempt that just failed, so the
    first failure waits `schedule[0]`.
    """
    index = attempts - 1
    if index < 0 or index >= len(schedule):
        return None
    return timedelta(minutes=schedule[index])


def fail(
    session: Session,
    task: Task,
    error: str,
    *,
    backoff_schedule: list[int],
    retry_after: timedelta | None = None,
) -> str:
    """Reschedule with backoff, or dead-letter once the schedule is exhausted.

    `retry_after` overrides the schedule so a 429's `Retry-After` is honoured
    rather than retried into a hard quota (§13.2).
    """
    delay = (
        retry_after if retry_after is not None else backoff_delay(task.attempts, backoff_schedule)
    )
    if delay is None or task.attempts >= task.max_attempts:
        session.execute(
            text(
                """
                UPDATE task_queue
                   SET status = 'dead', locked_at = NULL, locked_by = NULL,
                       last_error = :error, updated_at = now()
                 WHERE id = :id
                """
            ),
            {"id": task.id, "error": error[:2000]},
        )
        log.error("queue.dead_letter", task_id=str(task.id), task_type=task.task_type, error=error)
        return "dead"

    session.execute(
        text(
            """
            UPDATE task_queue
               SET status = 'pending', locked_at = NULL, locked_by = NULL,
                   run_after = now() + CAST(:delay AS interval),
                   last_error = :error, updated_at = now()
             WHERE id = :id
            """
        ),
        {"id": task.id, "delay": f"{int(delay.total_seconds())} seconds", "error": error[:2000]},
    )
    log.warning(
        "queue.retry_scheduled",
        task_id=str(task.id),
        task_type=task.task_type,
        attempts=task.attempts,
        delay_seconds=int(delay.total_seconds()),
    )
    return "pending"


def reap_stale(session: Session, *, lock_timeout_minutes: int = 30) -> int:
    """Return tasks whose worker died mid-run to the pending pool.

    A crash normally releases the row with its transaction; this covers the case
    where the process was killed after committing the claim.
    """
    result = session.execute(
        text(
            """
            UPDATE task_queue
               SET status = 'pending', locked_at = NULL, locked_by = NULL,
                   last_error = COALESCE(last_error, 'reclaimed after stale lock'),
                   updated_at = now()
             WHERE status = 'running'
               AND locked_at < now() - CAST(:timeout AS interval)
            """
        ),
        {"timeout": f"{lock_timeout_minutes} minutes"},
    )
    return rows_affected(result)


def depth(session: Session) -> dict[str, int]:
    """Queue depth by status — the cheapest useful worker metric."""
    rows = session.execute(
        text("SELECT status, count(*) AS n FROM task_queue GROUP BY status")
    ).all()
    return {row.status: int(row.n) for row in rows}


def _json(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, default=str)
