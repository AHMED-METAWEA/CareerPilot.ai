"""Administration endpoints (§12.6).

Phase 0 ships the four that the data spine needs: source health, a manual run
trigger, the company review queue and the unmapped-skill queue — plus corpus
statistics measured against the Phase 0 exit criteria.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import get_session, require_admin
from app.db.queue import TaskType, depth, enqueue
from app.services.sources import corpus_stats, source_health

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.get("/sources")
def list_sources(session: Session = Depends(get_session)) -> dict[str, Any]:
    """Source registry with health metrics."""
    rows = source_health(session)
    return {
        "sources": rows,
        "counts": {
            "enabled": sum(1 for r in rows if r["enabled"]),
            "disabled": sum(1 for r in rows if not r["enabled"]),
            "suspect": sum(1 for r in rows if r["last_status"] == "suspect"),
        },
    }


@router.post("/sources/{source_id}/run", status_code=status.HTTP_202_ACCEPTED)
def run_source(source_id: uuid.UUID, session: Session = Depends(get_session)) -> dict[str, Any]:
    """Queue a discovery run. Returns 202 with the task id, or 409 if already queued."""
    row = session.execute(
        text("SELECT name, enabled FROM job_sources WHERE id = :id"), {"id": source_id}
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Source not found")

    task_id = enqueue(
        session,
        TaskType.DISCOVER,
        {"source_id": str(source_id)},
        priority=20,
        dedup_key=f"discover:manual:{source_id}",
    )
    if task_id is None:
        raise HTTPException(status_code=409, detail="A run for this source is already queued")
    return {"task_id": str(task_id), "source": row.name, "status": "queued"}


@router.get("/companies/review")
def companies_review(
    limit: int = Query(default=50, le=200),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Employer references that need a human decision (§11.3, R5)."""
    rows = session.execute(
        text(
            """
            SELECT q.id, q.observed_name, q.observed_domain, q.confidence,
                   q.matched_alias, q.occurrences, q.created_at,
                   s.name AS source_name,
                   c.id AS suggested_company_id, c.canonical_name AS suggested_company
              FROM company_review_queue q
              LEFT JOIN job_sources s ON s.id = q.source_id
              LEFT JOIN companies c ON c.id = q.suggested_company_id
             WHERE q.status = 'pending'
             ORDER BY q.occurrences DESC, q.created_at
             LIMIT :limit
            """
        ),
        {"limit": limit},
    ).all()
    return {"pending": [dict(row._mapping) for row in rows], "count": len(rows)}


@router.get("/skills/unmapped")
def skills_unmapped(
    limit: int = Query(default=100, le=500),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Skill tokens that did not resolve to the taxonomy (§10.2)."""
    rows = session.execute(
        text(
            """
            SELECT id, token, lang, occurrences, first_seen_at, last_seen_at
              FROM unmapped_skills
             WHERE resolved_skill_id IS NULL
             ORDER BY occurrences DESC, last_seen_at DESC
             LIMIT :limit
            """
        ),
        {"limit": limit},
    ).all()
    return {"unmapped": [dict(row._mapping) for row in rows], "count": len(rows)}


@router.get("/stats")
def stats(session: Session = Depends(get_session)) -> dict[str, Any]:
    """Corpus statistics and queue depth."""
    data = corpus_stats(session)
    return {"corpus": _plain(data), "queue": depth(session)}


def _plain(data: dict[str, Any]) -> dict[str, Any]:
    """Decimal is not JSON; ints must stay ints so counts do not render as 0.0."""
    return {k: (float(v) if isinstance(v, Decimal) else v) for k, v in data.items()}
