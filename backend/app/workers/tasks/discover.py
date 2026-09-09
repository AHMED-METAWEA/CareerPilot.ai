"""Discovery, deduplication and corpus-maintenance tasks (§13).

`discover` is the only task that touches the network in Phase 0. It hands its
changed posting ids to `deduplicate` through the queue rather than calling it
inline, so a dedup bug cannot cost a fetch and a slow dedup cannot hold a
source's rate-limit budget open.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import text

from app.db.queue import TaskType, enqueue
from app.db.session import rows_affected
from app.domain.dedup.simhash import simhash64, to_signed
from app.services.dedup import DedupService
from app.services.ingestion import IngestionService
from app.workers.tasks.registry import TaskContext, handler

log = structlog.get_logger(__name__)

DEDUP_BATCH = 200
_PARTITION_NAME = re.compile(r"raw_payloads_\d{6}")


@handler(TaskType.DISCOVER)
def run_discover(ctx: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
    source_id = uuid.UUID(str(payload["source_id"]))
    service = IngestionService(ctx.session, ctx.http, ctx.config)
    report = service.run_source(source_id)

    for start in range(0, len(report.changed_posting_ids), DEDUP_BATCH):
        batch = report.changed_posting_ids[start : start + DEDUP_BATCH]
        enqueue(
            ctx.session,
            TaskType.DEDUPLICATE,
            {"posting_ids": [str(pid) for pid in batch]},
            priority=5,
            dedup_key=f"dedup:{report.run_id}:{start}",
        )

    log.info(
        "discover.completed",
        source=report.source_name,
        fetched=report.fetched,
        new=report.new,
        updated=report.updated,
        errors=report.errors,
        status=report.status,
    )
    return {
        "source": report.source_name,
        "fetched": report.fetched,
        "new": report.new,
        "updated": report.updated,
        "errors": report.errors,
        "status": report.status,
    }


@handler(TaskType.DEDUPLICATE)
def run_deduplicate(ctx: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
    posting_ids = [uuid.UUID(str(pid)) for pid in payload.get("posting_ids", [])]
    report = DedupService(ctx.session, ctx.config).deduplicate(posting_ids)
    return {
        "considered": report.considered,
        "compared": report.compared,
        "clusters": report.clusters,
        "merged": report.merged_postings,
        "reasons": report.reasons,
    }


@handler(TaskType.FETCH_DETAILS)
def run_fetch_details(ctx: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Second phase for sources whose list response carries no body.

    Rate-limited through the same polite client as discovery, and idempotent:
    re-running it simply rewrites the same descriptions.
    """
    from app.adapters.sources import build_adapter

    source_id = uuid.UUID(str(payload["source_id"]))
    external_ids: list[str] = [str(x) for x in payload.get("external_ids", [])]
    row = ctx.session.execute(
        text("SELECT adapter, name, config, rate_limit_rpm FROM job_sources WHERE id = :id"),
        {"id": source_id},
    ).one()
    adapter = build_adapter(
        row.adapter,
        name=row.name,
        config=row.config or {},
        http=ctx.http,
        rate_limit_rpm=row.rate_limit_rpm,
    )
    if not adapter.supports_details:
        return {"skipped": "adapter has no detail phase"}

    filled = 0
    for external_id in external_ids:
        detail = adapter.fetch_detail(external_id)  # type: ignore[attr-defined]
        if not detail:
            continue
        body = adapter.describe(detail)  # type: ignore[attr-defined]
        if not body:
            continue
        result = ctx.session.execute(
            text(
                """
                UPDATE job_postings
                   SET description_text = :body,
                       content_simhash = :simhash,
                       updated_at = now()
                 WHERE source_id = :source_id AND external_id = :external_id
                """
            ),
            {
                "body": body,
                "simhash": to_signed(simhash64(body)),
                "source_id": source_id,
                "external_id": external_id,
            },
        )
        filled += rows_affected(result)

    log.info("fetch_details.completed", source=row.name, requested=len(external_ids), filled=filled)
    return {"source": row.name, "requested": len(external_ids), "filled": filled}


@handler(TaskType.EXPIRE_POSTINGS)
def run_expire_postings(ctx: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Close postings that have expired or whose apply URL is gone (§13).

    Age alone never closes a posting: the freshness *gate* handles staleness at
    match time, and an employer that leaves a role open for 60 days has not
    withdrawn it.
    """
    result = ctx.session.execute(
        text(
            """
            UPDATE job_postings
               SET status = 'expired', updated_at = now()
             WHERE status = 'open'
               AND (
                     (expires_at IS NOT NULL AND expires_at < now())
                  OR url_status = 'gone'
               )
            """
        )
    )
    closed = rows_affected(result)
    log.info("expire_postings.completed", closed=closed)
    return {"closed": closed}


@handler(TaskType.PRUNE_RAW)
def run_prune_raw(ctx: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Maintain the rolling raw_payloads window (§6.4).

    Creates the next months' partitions before they are needed and drops those
    wholly older than the retention window. Dropping a partition is a metadata
    operation; deleting the same rows is hours of vacuum.

    Written as Python plus plain SQL rather than a PL/pgSQL block: a `DO $$ … $$`
    body is a string literal to Postgres, so a bind parameter inside it is never
    substituted — the task failed on its first real run because of exactly that.
    Partition names are built here and validated against a strict pattern before
    they reach a statement.
    """
    retention_days = ctx.config.ingestion.raw_payload_retention_days
    today = datetime.now(UTC).date()

    created: list[str] = []
    month = today.replace(day=1)
    for _ in range(3):
        following = _next_month(month)
        name = f"raw_payloads_{month:%Y%m}"
        ctx.session.execute(
            text(
                f'CREATE TABLE IF NOT EXISTS "{_validated(name)}" PARTITION OF raw_payloads '
                f"FOR VALUES FROM ('{month.isoformat()}') TO ('{following.isoformat()}')"
            )
        )
        created.append(name)
        month = following

    cutoff = today - timedelta(days=retention_days)
    existing = (
        ctx.session.execute(
            text(
                """
                SELECT c.relname
                  FROM pg_class c
                  JOIN pg_inherits i ON i.inhrelid = c.oid
                 WHERE i.inhparent = 'raw_payloads'::regclass
                   AND c.relname ~ '^raw_payloads_[0-9]{6}$'
                """
            )
        )
        .scalars()
        .all()
    )

    dropped: list[str] = []
    for name in existing:
        start_of_month = date(int(name[-6:-2]), int(name[-2:]), 1)
        # Drop only when the partition's whole range is past the window.
        if _next_month(start_of_month) <= cutoff:
            ctx.session.execute(text(f'DROP TABLE IF EXISTS "{_validated(name)}"'))
            dropped.append(name)

    log.info(
        "prune_raw.completed",
        retention_days=retention_days,
        created=created,
        dropped=dropped,
    )
    return {"retention_days": retention_days, "created": created, "dropped": dropped}


def _next_month(day: date) -> date:
    return date(day.year + (day.month == 12), day.month % 12 + 1, 1)


def _validated(name: str) -> str:
    """Partition names are constructed here; this asserts they stayed that way."""
    if not _PARTITION_NAME.fullmatch(name):
        raise ValueError(f"refusing to use unexpected partition name: {name!r}")
    return name
