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
