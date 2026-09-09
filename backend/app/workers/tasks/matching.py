"""Phase 1 background jobs: embed, verify, match, digest-free (§13).

Each is a thin wrapper over a service. The wrappers exist so the schedule, the
retry policy and the transaction boundary live in one place, and the services
stay callable from the CLI and the API without a queue in between.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import text

from app.adapters.embeddings import build_embedder, build_reranker
from app.adapters.llm import build_provider
from app.adapters.llm.base import ChatProvider, LLMError
from app.config import Settings, get_settings
from app.db.queue import TaskType, enqueue
from app.services.embedding import EmbeddingService
from app.services.matching import MatchingService
from app.services.verification import VerificationService
from app.workers.tasks.registry import TaskContext, handler

log = structlog.get_logger(__name__)

VERIFY_BATCH = 60
EMBED_BATCH = 256


@handler(TaskType.EMBED)
def run_embed(ctx: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Document and sentence vectors for whatever lacks them (§7.2)."""
    embedder = build_embedder(ctx.config.models.embedding)
    report = EmbeddingService(ctx.session, embedder).embed_pending(batch_size=EMBED_BATCH)

    # Re-queue while there is more to do, rather than holding one long task.
    if report.postings_embedded >= EMBED_BATCH or report.requirements_embedded >= EMBED_BATCH:
        enqueue(ctx.session, TaskType.EMBED, priority=2, dedup_key=None)

    return {
        "postings": report.postings_embedded,
        "requirements": report.requirements_embedded,
        "model": report.model,
    }


@handler(TaskType.VERIFY_URLS)
def run_verify_urls(ctx: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Liveness verification, oldest first (§11.5).

    A posting is never shown without a `live` status inside the window, so this
    task is what keeps the corpus visible at all — not merely tidy.
    """
    limit = int(payload.get("limit", VERIFY_BATCH))
    report = VerificationService(ctx.session, ctx.http, ctx.config).verify_batch(limit=limit)

    remaining = ctx.session.execute(
        text(
            """
            SELECT count(*) FROM job_postings
             WHERE status = 'open'
               AND (last_verified_at IS NULL
                    OR last_verified_at < now() - CAST(:window AS interval))
            """
        ),
        {"window": f"{ctx.config.verification.reverify_after_hours} hours"},
    ).scalar_one()

    if report.checked and remaining:
        enqueue(ctx.session, TaskType.VERIFY_URLS, {"limit": limit}, priority=4)

    return {
        "checked": report.checked,
        "live": report.live,
        "gone": report.gone,
        "blocked": report.blocked,
        "redirected": report.redirected,
        "unknown": report.unknown,
        "remaining": int(remaining),
    }


@handler(TaskType.MATCH_USERS)
def run_match_users(ctx: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
    """The funnel, for one profile or for every active profile (§11.4).

    Runs are staggered across profiles by queueing one task each rather than
    looping here: a per-minute inference limit is easier to respect from the
    queue than from inside a single long task (R1).
    """
    profile_id = payload.get("profile_id")
    if profile_id is None:
        rows = (
            ctx.session.execute(
                text(
                    """
                    SELECT DISTINCT ON (user_id) id
                      FROM candidate_profiles
                     ORDER BY user_id, extracted_at DESC
                    """
                )
            )
            .scalars()
            .all()
        )
        for identifier in rows:
            enqueue(
                ctx.session,
                TaskType.MATCH_USERS,
                {"profile_id": str(identifier)},
                priority=6,
                dedup_key=f"match:{identifier}",
            )
        return {"queued_profiles": len(rows)}

    service = MatchingService(
        ctx.session,
        ctx.config,
        embedder=build_embedder(ctx.config.models.embedding),
        reranker=build_reranker(ctx.config.models.reranker),
        llm=_optional_llm(ctx, get_settings()),
    )
    report = service.run_for_profile(uuid.UUID(str(profile_id)))
    return {
        "profile_id": str(profile_id),
        "eligible": report.eligible,
        "finalists": report.finalists,
        "persisted": report.persisted,
        "withheld": report.withheld,
        "used_llm": report.used_llm,
        "tokens": report.tokens_in + report.tokens_out,
    }


@handler(TaskType.DIGEST)
def run_digest(ctx: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Compose and send the daily digest (§11.7).

    One task per user, queued by the same handler with no payload, so a slow or
    failing send for one person cannot hold up everybody else's.
    """
    from app.services.digest import DigestService

    service = DigestService(ctx.session)
    user_id = payload.get("user_id")

    if user_id is None:
        due = service.due_users()
        for identifier in due:
            enqueue(
                ctx.session,
                TaskType.DIGEST,
                {"user_id": str(identifier)},
                priority=3,
                dedup_key=f"digest:{identifier}:{datetime.now(UTC):%Y-%m-%d}",
            )
        return {"queued_users": len(due)}

    return service.send(uuid.UUID(str(user_id)))


def _optional_llm(ctx: TaskContext, settings: Settings) -> ChatProvider | None:
    """A provider if one is configured, otherwise None.

    Matching degrades to deterministic requirement extraction rather than
    failing: a missing key should cost explanation quality, not the run.
    """
    try:
        return build_provider(settings, ctx.http, ctx.config.models.fallbacks)
    except LLMError as exc:
        log.info("match.no_llm_provider", reason=str(exc))
        return None
