"""Worker loop: claim, execute, complete, retry (§13)."""

from __future__ import annotations

import httpx
import pytest
import respx
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.config import get_config
from app.db.queue import TaskType, enqueue
from app.workers.runner import Worker, enqueue_due_discoveries
from tests.conftest import load_fixture

pytestmark = pytest.mark.db


@pytest.fixture
def worker(db_session: Session) -> Worker:
    runner = Worker(config=get_config(), concurrency=1)
    # Tests must not sleep between hosts.
    runner.http = __import__("app.adapters.http", fromlist=["HttpClient"]).HttpClient(
        user_agent="CareerPilotBot/test", per_host_min_interval_seconds=0.0, max_retries=1
    )
    yield runner
    runner.shutdown()
    runner.http.close()


@respx.mock
def test_worker_runs_a_discovery_task(db_session: Session, source_row, worker: Worker) -> None:
    respx.get(url__startswith="https://boards-api.greenhouse.io").mock(
        return_value=httpx.Response(200, json=load_fixture("greenhouse", "board.json"))
    )
    source_id = source_row()
    enqueue(db_session, TaskType.DISCOVER, {"source_id": str(source_id)})
    db_session.commit()

    assert worker.run_once() is True
    assert db_session.execute(sa.text("SELECT count(*) FROM job_postings")).scalar_one() == 2
    assert (
        db_session.execute(
            sa.text("SELECT status FROM task_queue WHERE task_type = 'discover'")
        ).scalar_one()
        == "done"
    )


@respx.mock
def test_discovery_queues_deduplication(db_session: Session, source_row, worker: Worker) -> None:
    """Dedup runs through the queue, so a dedup bug cannot cost a fetch."""
    respx.get(url__startswith="https://boards-api.greenhouse.io").mock(
        return_value=httpx.Response(200, json=load_fixture("greenhouse", "board.json"))
    )
    enqueue(db_session, TaskType.DISCOVER, {"source_id": str(source_row())})
    db_session.commit()
    worker.run_once()

    queued = (
        db_session.execute(
            sa.text("SELECT payload FROM task_queue WHERE task_type = 'deduplicate'")
        )
        .scalars()
        .all()
    )
    assert queued and len(queued[0]["posting_ids"]) == 2

    assert worker.run_once() is True  # the dedup task itself
    assert (
        db_session.execute(
            sa.text("SELECT count(*) FROM job_postings WHERE job_group_id IS NULL")
        ).scalar_one()
        == 0
    )


def test_idle_queue_returns_false(worker: Worker) -> None:
    assert worker.run_once() is False


def test_unknown_task_type_is_retried_then_dead_lettered(
    db_session: Session, worker: Worker
) -> None:
    enqueue(db_session, "no_such_task", {}, max_attempts=1)
    db_session.commit()

    assert worker.run_once() is True
    status = db_session.execute(sa.text("SELECT status FROM task_queue")).scalar_one()
    assert status == "dead"


@respx.mock
def test_failed_task_is_rescheduled_not_lost(
    db_session: Session, source_row, worker: Worker
) -> None:
    respx.get(url__startswith="https://boards-api.greenhouse.io").mock(
        return_value=httpx.Response(500)
    )
    enqueue(db_session, TaskType.DISCOVER, {"source_id": str(source_row())}, max_attempts=3)
    db_session.commit()

    worker.run_once()
    row = db_session.execute(sa.text("SELECT status, attempts, last_error FROM task_queue")).one()
    assert row.status == "pending" and row.attempts == 1 and row.last_error


def test_scheduler_enqueues_due_sources_once_per_interval(db_session: Session, source_row) -> None:
    source_row()
    assert enqueue_due_discoveries(db_session) == 1
    db_session.commit()
    # Same interval bucket: no duplicate.
    assert enqueue_due_discoveries(db_session) == 0
    db_session.commit()


def test_scheduler_skips_disabled_sources(db_session: Session, source_row) -> None:
    source_id = source_row()
    db_session.execute(
        sa.text("UPDATE job_sources SET enabled = false WHERE id = :id"), {"id": source_id}
    )
    db_session.commit()
    assert enqueue_due_discoveries(db_session) == 0


@respx.mock
def test_fetch_details_fills_bodies_and_is_idempotent(
    db_session: Session, source_row, worker: Worker
) -> None:
    """The second phase for sources whose list response carries no body."""
    detail = load_fixture("smartrecruiters", "detail.json")
    respx.get(
        url__startswith="https://api.smartrecruiters.com/v1/companies/McDonaldsCorporation/postings"
    ).mock(
        side_effect=lambda request: httpx.Response(
            200,
            json=detail
            if request.url.path.rstrip("/").split("/")[-1] not in {"postings"}
            else load_fixture("smartrecruiters", "board.json"),
        )
    )
    source_id = source_row(
        adapter="smartrecruiters",
        name="smartrecruiters:mcd",
        company_id="McDonaldsCorporation",
        company_name="McDonald's",
    )
    enqueue(db_session, TaskType.DISCOVER, {"source_id": str(source_id)})
    db_session.commit()

    worker.run_once()  # discover: list rows only, bodies still empty
    assert (
        db_session.execute(
            sa.text("SELECT count(*) FROM job_postings WHERE description_text = ''")
        ).scalar_one()
        == 2
    )

    while worker.run_once():  # dedup, then the queued detail fetches
        pass

    filled = db_session.execute(
        sa.text("SELECT count(*) FROM job_postings WHERE description_text <> ''")
    ).scalar_one()
    assert filled == 2

    simhashes = (
        db_session.execute(sa.text("SELECT content_simhash FROM job_postings")).scalars().all()
    )
    assert all(value is not None and value != 0 for value in simhashes), (
        "a filled body must update the SimHash, or dedup compares an empty hash"
    )


def test_fetch_details_on_an_adapter_without_a_detail_phase(
    db_session: Session, source_row, worker: Worker
) -> None:
    source_id = source_row()
    enqueue(
        db_session,
        TaskType.FETCH_DETAILS,
        {"source_id": str(source_id), "external_ids": ["1"]},
    )
    db_session.commit()

    assert worker.run_once() is True
    assert (
        db_session.execute(
            sa.text("SELECT status FROM task_queue WHERE task_type = 'fetch_details'")
        ).scalar_one()
        == "done"
    )


# ── Scheduler wiring (§11.5, §13) ─────────────────────────────────────
#
# Both cases below shipped broken and failed silently. Nothing errored; the
# corpus simply went stale and every posting was gated out of every shortlist,
# which is only visible by inspecting the data rather than the logs.


def _scheduled_jobs() -> dict[str, object]:
    runner = Worker(config=get_config(), concurrency=1)
    try:
        runner._scheduler.start(paused=True)
        runner._schedule_jobs()
        return {job.id: job for job in runner._scheduler.get_jobs()}
    finally:
        if runner._scheduler.running:
            runner._scheduler.shutdown(wait=False)


def test_no_scheduled_job_is_added_paused() -> None:
    """`next_run_time=None` does not mean "use the default" — it means paused.

    Hourly discovery passed it and therefore never ran on a schedule at all,
    which is why the corpus never met the six-hour freshness criterion.
    """
    jobs = _scheduled_jobs()
    paused = [job_id for job_id, job in jobs.items() if getattr(job, "next_run_time", None) is None]
    assert paused == [], f"these jobs would never fire: {paused}"


def test_the_jobs_that_keep_the_corpus_visible_run_soon_after_boot() -> None:
    """An interval trigger's first run is one whole interval away.

    URL verification is twelve-hourly, so it needed twelve hours of unbroken
    uptime to fire even once — and a restart put it back to the start. §11.5
    makes verification the thing that keeps postings visible, so on a host that
    restarts daily nothing was ever verified and no shortlist had anything in
    it. Discovery has the same shape of problem on a smaller scale.
    """
    from datetime import UTC, datetime, timedelta

    jobs = _scheduled_jobs()
    soon = datetime.now(UTC) + timedelta(minutes=5)

    for job_id in ("verify_urls", "discover"):
        job = jobs[job_id]
        next_run = getattr(job, "next_run_time", None)
        assert next_run is not None, f"{job_id} is paused"
        assert next_run <= soon, (
            f"{job_id} does not run until {next_run}; a restart before then means it never runs"
        )
