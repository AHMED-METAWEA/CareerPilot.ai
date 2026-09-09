"""Worker process: scheduler plus a pool of queue consumers (§13).

Claiming and executing are deliberately separate transactions. The claim
commits immediately so the row lock is not held for the length of a fetch;
execution then runs in its own transaction, which either commits with the task
marked done or rolls back and leaves the task to the backoff schedule.
"""

from __future__ import annotations

import signal
import threading
import time
import uuid
from datetime import timedelta
from types import FrameType
from typing import Any

import structlog
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.adapters.http import HttpClient, RateLimitedError
from app.config import AppConfig, get_config, get_settings
from app.db.queue import WORKER_ID, TaskType, claim, complete, enqueue, fail, reap_stale
from app.db.session import session_scope
from app.logging import configure_logging
from app.workers.tasks import HANDLERS, TaskContext

log = structlog.get_logger(__name__)

IDLE_SLEEP_SECONDS = 2.0


def enqueue_due_discoveries(session: Session, *, interval_minutes: int = 60) -> int:
    """Enqueue one discovery per enabled source that is due (§13, hourly).

    The dedup key is bucketed by the interval, so a scheduler restart or an
    overlapping tick cannot queue the same source twice.
    """
    rows = session.execute(
        text(
            """
            SELECT id, name, tier
              FROM job_sources
             WHERE enabled
               AND (last_run_at IS NULL
                    OR last_run_at < now() - CAST(:interval AS interval))
             ORDER BY last_run_at NULLS FIRST
            """
        ),
        {"interval": f"{interval_minutes} minutes"},
    ).all()

    queued = 0
    bucket = int(time.time() // (interval_minutes * 60))
    for row in rows:
        task_id = enqueue(
            session,
            TaskType.DISCOVER,
            {"source_id": str(row.id)},
            # Tier 1 (the employer's own ATS) is the authoritative feed and runs
            # ahead of aggregators when the queue is backed up.
            priority=10 if row.tier == 1 else 0,
            dedup_key=f"discover:{row.id}:{bucket}",
        )
        if task_id is not None:
            queued += 1
    if queued:
        log.info("scheduler.discoveries_enqueued", count=queued)
    return queued


class Worker:
    def __init__(self, config: AppConfig | None = None, *, concurrency: int | None = None) -> None:
        self.config = config or get_config()
        self.concurrency = concurrency or self.config.queue.worker_concurrency
        self.http = HttpClient(
            user_agent=self.config.ingestion.user_agent,
            timeout_seconds=self.config.ingestion.request_timeout_seconds,
            max_retries=self.config.ingestion.max_retries,
            per_host_min_interval_seconds=self.config.verification.per_host_min_interval_seconds,
        )
        self._stop = threading.Event()
        self._scheduler = BackgroundScheduler(timezone="UTC")

    # ── lifecycle ────────────────────────────────────────────────────

    def start(self) -> None:
        self._install_signal_handlers()
        self._schedule_jobs()
        self._scheduler.start()
        log.info("worker.started", worker_id=WORKER_ID, concurrency=self.concurrency)

        threads = [
            threading.Thread(target=self._consume, name=f"consumer-{i}", daemon=True)
            for i in range(self.concurrency)
        ]
        for thread in threads:
            thread.start()
        try:
            while not self._stop.is_set():
                self._stop.wait(1.0)
        finally:
            # Order matters: stop accepting work, let in-flight tasks finish,
            # and only then close the HTTP client. Closing it first fails the
            # task that is mid-fetch — recoverable, since the queue retries, but
            # a needless failure on every deploy.
            self.shutdown()
            for thread in threads:
                thread.join(timeout=60)
            self.http.close()
            log.info("worker.stopped", worker_id=WORKER_ID)

    def shutdown(self) -> None:
        """Stop accepting work. In-flight tasks are allowed to finish."""
        if not self._stop.is_set():
            self._stop.set()
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def _install_signal_handlers(self) -> None:
        def handle(signum: int, _: FrameType | None) -> None:
            log.info("worker.signal", signal=signum)
            self._stop.set()

        try:
            signal.signal(signal.SIGTERM, handle)
            signal.signal(signal.SIGINT, handle)
        except ValueError:
            # Only the main thread can install handlers. Embedded runs (tests,
            # a supervisor thread) stop through `shutdown()` instead.
            log.debug("worker.signal_handlers_unavailable")

    def _schedule_jobs(self) -> None:
        self._scheduler.add_job(
            self._scheduled(lambda session: enqueue_due_discoveries(session)),
            "interval",
            minutes=60,
            id="discover",
            next_run_time=None,
            coalesce=True,
            max_instances=1,
        )
        self._scheduler.add_job(
            self._scheduled(
                lambda session: reap_stale(
                    session, lock_timeout_minutes=self.config.queue.lock_timeout_minutes
                )
            ),
            "interval",
            minutes=5,
            id="reap_stale",
            coalesce=True,
            max_instances=1,
        )
        # Embedding keeps the vector half of retrieval usable; batched rather
        # than per-posting so a burst of discovery does not become a burst of
        # model calls (§13).
        self._scheduler.add_job(
            self._scheduled(
                lambda session: enqueue(session, TaskType.EMBED, dedup_key="embed", priority=2)
            ),
            "interval",
            minutes=15,
            id="embed",
            coalesce=True,
            max_instances=1,
        )
        # Every 12 hours, oldest first. Postings unverified for 48 hours are
        # suppressed from display, so this is what keeps the corpus visible.
        self._scheduler.add_job(
            self._scheduled(
                lambda session: enqueue(
                    session, TaskType.VERIFY_URLS, dedup_key="verify_urls", priority=4
                )
            ),
            "interval",
            hours=12,
            id="verify_urls",
            coalesce=True,
            max_instances=1,
        )
        # Nightly at 02:00, staggered per profile by the task itself.
        self._scheduler.add_job(
            self._scheduled(
                lambda session: enqueue(
                    session, TaskType.MATCH_USERS, dedup_key="match_users", priority=6
                )
            ),
            "cron",
            hour=2,
            minute=0,
            id="match_users",
        )
        self._scheduler.add_job(
            self._scheduled(
                lambda session: enqueue(
                    session, TaskType.EXPIRE_POSTINGS, dedup_key="expire_postings"
                )
            ),
            "cron",
            hour=3,
            minute=0,
            id="expire_postings",
        )
        self._scheduler.add_job(
            self._scheduled(
                lambda session: enqueue(session, TaskType.PRUNE_RAW, dedup_key="prune_raw")
            ),
            "cron",
            day_of_week="sun",
            hour=4,
            minute=0,
            id="prune_raw",
        )

    @staticmethod
    def _scheduled(fn: Any) -> Any:
        def wrapped() -> None:
            try:
                with session_scope() as session:
                    fn(session)
            except Exception as exc:  # a scheduler thread must never die
                log.exception("scheduler.job_failed", error=str(exc))

        return wrapped

    # ── consumption ──────────────────────────────────────────────────

    def _consume(self) -> None:
        while not self._stop.is_set():
            try:
                if not self.run_once():
                    self._stop.wait(IDLE_SLEEP_SECONDS)
            except Exception as exc:  # consumer loops are restarted, never abandoned
                log.exception("worker.loop_error", error=str(exc))
                self._stop.wait(IDLE_SLEEP_SECONDS)

    def run_once(self) -> bool:
        """Claim and execute at most one task. Returns False when the queue is idle."""
        with session_scope() as session:
            task = claim(session)
        if task is None:
            return False

        handler = HANDLERS.get(task.task_type)
        if handler is None:
            with session_scope() as session:
                fail(
                    session,
                    task,
                    f"no handler registered for '{task.task_type}'",
                    backoff_schedule=self.config.queue.backoff_minutes,
                )
            return True

        log.info(
            "task.started", task_id=str(task.id), task_type=task.task_type, attempt=task.attempts
        )
        try:
            with session_scope() as session:
                context = TaskContext(session=session, http=self.http, config=self.config)
                result = handler(context, task.payload)
                complete(session, task.id)
        except RateLimitedError as exc:
            # Honour Retry-After rather than burning the source's budget (§13.2).
            with session_scope() as session:
                fail(
                    session,
                    task,
                    str(exc),
                    backoff_schedule=self.config.queue.backoff_minutes,
                    retry_after=timedelta(seconds=exc.retry_after) if exc.retry_after else None,
                )
            return True
        except Exception as exc:
            with session_scope() as session:
                fail(
                    session,
                    task,
                    f"{type(exc).__name__}: {exc}",
                    backoff_schedule=self.config.queue.backoff_minutes,
                )
            log.warning(
                "task.failed", task_id=str(task.id), task_type=task.task_type, error=str(exc)
            )
            return True

        log.info("task.completed", task_id=str(task.id), task_type=task.task_type, result=result)
        return True

    def run_source_now(self, source_id: uuid.UUID) -> uuid.UUID | None:
        """Queue one source immediately — used by the admin endpoint and the CLI."""
        with session_scope() as session:
            return enqueue(
                session,
                TaskType.DISCOVER,
                {"source_id": str(source_id)},
                priority=20,
                dedup_key=f"discover:manual:{source_id}:{int(time.time() // 60)}",
            )


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json_output=settings.careerpilot_env != "local")
    Worker().start()


if __name__ == "__main__":
    main()
