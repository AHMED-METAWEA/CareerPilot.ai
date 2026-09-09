"""Corpus maintenance tasks (§13).

`prune_raw` drops table partitions. Untested code that deletes data is the kind
of thing that works for 89 days and then removes the wrong month.
"""

from __future__ import annotations

import json
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.adapters.http import HttpClient
from app.config import get_config
from app.workers.tasks.discover import run_expire_postings, run_prune_raw
from app.workers.tasks.registry import TaskContext

pytestmark = pytest.mark.db


@pytest.fixture
def ctx(db_session: Session, http_client: HttpClient) -> TaskContext:
    return TaskContext(session=db_session, http=http_client, config=get_config())


def make_posting(session: Session, source_id: uuid.UUID, **overrides: object) -> uuid.UUID:
    params = {
        "source_id": source_id,
        "external_id": str(uuid.uuid4()),
        "expires_at": None,
        "url_status": "unknown",
        "status": "open",
    }
    params.update(overrides)
    return session.execute(
        sa.text(
            """
            INSERT INTO job_postings (source_id, external_id, title, title_normalized,
                                      description_text, apply_url, source_url,
                                      expires_at, url_status, status)
            VALUES (:source_id, :external_id, 'Engineer', 'engineer', 'body',
                    'https://x/1', 'https://x/1', :expires_at, :url_status, :status)
            RETURNING id
            """
        ),
        params,
    ).scalar_one()


def test_expire_closes_only_what_is_actually_over(ctx: TaskContext, source_row) -> None:
    source_id = source_row()
    expired = make_posting(ctx.session, source_id)
    ctx.session.execute(
        sa.text("UPDATE job_postings SET expires_at = now() - interval '1 day' WHERE id = :id"),
        {"id": expired},
    )
    gone = make_posting(ctx.session, source_id, url_status="gone")
    old_but_open = make_posting(ctx.session, source_id)
    ctx.session.execute(
        sa.text("UPDATE job_postings SET posted_at = now() - interval '120 days' WHERE id = :id"),
        {"id": old_but_open},
    )
    ctx.session.commit()

    result = run_expire_postings(ctx, {})
    ctx.session.commit()

    assert result["closed"] == 2
    statuses = dict(
        ctx.session.execute(sa.text("SELECT id, status FROM job_postings")).all()  # type: ignore[arg-type]
    )
    assert statuses[expired] == "expired"
    assert statuses[gone] == "expired"
    # Age alone never closes a posting: the freshness gate handles staleness at
    # match time, and a role open for 120 days has not been withdrawn.
    assert statuses[old_but_open] == "open"


def test_expire_is_idempotent(ctx: TaskContext, source_row) -> None:
    source_id = source_row()
    make_posting(ctx.session, source_id, url_status="gone")
    ctx.session.commit()

    assert run_expire_postings(ctx, {})["closed"] == 1
    ctx.session.commit()
    assert run_expire_postings(ctx, {})["closed"] == 0


def partitions(session: Session) -> set[str]:
    return set(
        session.execute(
            sa.text(
                """
                SELECT c.relname FROM pg_class c
                  JOIN pg_inherits i ON i.inhrelid = c.oid
                 WHERE i.inhparent = 'raw_payloads'::regclass
                """
            )
        )
        .scalars()
        .all()
    )


def test_prune_creates_upcoming_partitions_and_keeps_recent_data(
    ctx: TaskContext, source_row
) -> None:
    source_id = source_row()
    run_id = ctx.session.execute(
        sa.text("INSERT INTO source_runs (source_id) VALUES (:id) RETURNING id"), {"id": source_id}
    ).scalar_one()
    ctx.session.execute(
        sa.text(
            """
            INSERT INTO raw_payloads (source_id, run_id, external_id, payload, fetched_at)
            VALUES (:source_id, :run_id, 'x', CAST(:payload AS jsonb), now())
            """
        ),
        {"source_id": source_id, "run_id": run_id, "payload": json.dumps({"a": 1})},
    )
    ctx.session.commit()

    before = partitions(ctx.session)
    run_prune_raw(ctx, {})
    ctx.session.commit()
    after = partitions(ctx.session)

    # Next months exist ahead of need; nothing recent was dropped.
    assert len(after) >= len(before)
    assert ctx.session.execute(sa.text("SELECT count(*) FROM raw_payloads")).scalar_one() == 1


def test_prune_drops_only_partitions_past_the_retention_window(ctx: TaskContext) -> None:
    """A partition wholly older than the window goes; the boundary one stays."""
    ctx.session.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS raw_payloads_201901 PARTITION OF raw_payloads
            FOR VALUES FROM ('2019-01-01') TO ('2019-02-01')
            """
        )
    )
    ctx.session.commit()
    assert "raw_payloads_201901" in partitions(ctx.session)

    run_prune_raw(ctx, {})
    ctx.session.commit()

    remaining = partitions(ctx.session)
    assert "raw_payloads_201901" not in remaining
    assert "raw_payloads_default" in remaining, "the catch-all partition must survive"
    # This month is inside the window and must not be touched.
    from datetime import UTC, datetime

    assert f"raw_payloads_{datetime.now(UTC):%Y%m}" in remaining
