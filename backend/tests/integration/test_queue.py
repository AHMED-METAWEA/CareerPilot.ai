"""The Postgres task queue (§13.1, §13.2)."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db.queue import claim, complete, depth, enqueue, fail, reap_stale
from app.db.session import get_sessionmaker

pytestmark = pytest.mark.db


def test_enqueue_and_claim(db_session: Session) -> None:
    task_id = enqueue(db_session, "discover", {"source_id": "abc"})
    db_session.commit()
    assert task_id is not None

    task = claim(db_session)
    assert task is not None
    assert task.task_type == "discover"
    assert task.payload == {"source_id": "abc"}
    assert task.attempts == 1


def test_dedup_key_makes_enqueue_idempotent(db_session: Session) -> None:
    """A scheduler tick that fires twice must not discover a source twice."""
    first = enqueue(db_session, "discover", {"source_id": "abc"}, dedup_key="discover:abc:1")
    second = enqueue(db_session, "discover", {"source_id": "abc"}, dedup_key="discover:abc:1")
    db_session.commit()
    assert first is not None and second is None
    assert depth(db_session)["pending"] == 1


def test_dedup_key_frees_up_once_the_task_is_done(db_session: Session) -> None:
    task_id = enqueue(db_session, "discover", {}, dedup_key="k")
    db_session.commit()
    assert task_id is not None
    complete(db_session, task_id)
    db_session.commit()
    assert enqueue(db_session, "discover", {}, dedup_key="k") is not None


def test_two_workers_never_claim_the_same_task(db_session: Session) -> None:
    """SKIP LOCKED is the whole reason this queue needs no extra infrastructure."""
    enqueue(db_session, "discover", {"n": 1})
    db_session.commit()

    session_a = get_sessionmaker()()
    session_b = get_sessionmaker()()
    try:
        claimed_a = claim(session_a, worker_id="a")
        claimed_b = claim(session_b, worker_id="b")
        assert claimed_a is not None
        assert claimed_b is None  # skipped the locked row rather than blocking
        session_a.commit()
        session_b.commit()
    finally:
        session_a.close()
        session_b.close()


def test_priority_and_run_after_order_the_queue(db_session: Session) -> None:
    enqueue(db_session, "discover", {"n": "low"}, priority=0)
    enqueue(db_session, "discover", {"n": "high"}, priority=10)
    db_session.commit()
    task = claim(db_session)
    assert task is not None and task.payload["n"] == "high"


def test_failure_backs_off_then_dead_letters(db_session: Session) -> None:
    enqueue(db_session, "discover", {}, max_attempts=3)
    db_session.commit()

    for expected_status in ("pending", "pending", "dead"):
        task = claim(db_session)
        assert task is not None
        status = fail(db_session, task, "boom", backoff_schedule=[1, 5, 25])
        db_session.commit()
        assert status == expected_status
        if status == "pending":
            # The retry is scheduled in the future, so it is not claimable now.
            assert claim(db_session) is None
            db_session.execute(
                sa.text("UPDATE task_queue SET run_after = now() - interval '1 minute'")
            )
            db_session.commit()

    row = db_session.execute(sa.text("SELECT status, last_error FROM task_queue")).one()
    assert row.status == "dead" and "boom" in row.last_error


def test_retry_after_overrides_the_backoff_schedule(db_session: Session) -> None:
    """A 429's Retry-After is honoured rather than retried into a hard quota."""
    from datetime import timedelta

    enqueue(db_session, "discover", {})
    db_session.commit()
    task = claim(db_session)
    assert task is not None
    fail(db_session, task, "429", backoff_schedule=[1, 5, 25], retry_after=timedelta(seconds=900))
    db_session.commit()

    delay = db_session.execute(
        sa.text("SELECT extract(epoch FROM run_after - now()) FROM task_queue")
    ).scalar_one()
    assert 800 < float(delay) <= 900


def test_reap_stale_returns_abandoned_tasks(db_session: Session) -> None:
    """A worker killed mid-task must not strand the row forever."""
    enqueue(db_session, "discover", {})
    db_session.commit()
    task = claim(db_session)
    assert task is not None
    db_session.execute(sa.text("UPDATE task_queue SET locked_at = now() - interval '2 hours'"))
    db_session.commit()

    assert reap_stale(db_session, lock_timeout_minutes=30) == 1
    db_session.commit()
    assert claim(db_session) is not None
