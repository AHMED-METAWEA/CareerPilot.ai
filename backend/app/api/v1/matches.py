"""Ranked shortlist and evidence (§12.3).

Two rules from §8.5 are enforced in this layer, not left to the client:

* a score is presented as a percentile within the candidate's own pool;
* nothing is described as a probability of an interview or an offer.

`apply_url` is returned exactly as stored — never shortened, proxied, wrapped
or rewritten (§12.7).
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import current_user_id, get_session
from app.db.queue import TaskType, enqueue
from app.domain.matching.gates import HUMAN_READABLE, GateFailure
from app.domain.scoring.percentile import Percentile
from app.services.matching import MODEL_VERSION

router = APIRouter(prefix="/matches", tags=["matches"])


@router.post("/refresh", status_code=status.HTTP_202_ACCEPTED)
def refresh(
    user_id: uuid.UUID = Depends(current_user_id),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Queue a matching run for the caller's newest profile."""
    profile_id = session.execute(
        text(
            "SELECT id FROM candidate_profiles WHERE user_id = :user_id "
            "ORDER BY extracted_at DESC LIMIT 1"
        ),
        {"user_id": user_id},
    ).scalar_one_or_none()
    if profile_id is None:
        raise HTTPException(status_code=404, detail="No profile yet; upload a CV first")

    task_id = enqueue(
        session,
        TaskType.MATCH_USERS,
        {"profile_id": str(profile_id)},
        priority=15,
        dedup_key=f"match:{profile_id}",
    )
    if task_id is None:
        raise HTTPException(status_code=409, detail="A matching run is already queued")
    return {"job_id": str(task_id), "profile_id": str(profile_id), "status": "queued"}


@router.get("")
def list_matches(
    min_percentile: float = Query(default=0.0, ge=0, le=100),
    remote: bool | None = Query(default=None),
    limit: int = Query(default=20, le=100),
    cursor: int = Query(default=0, ge=0),
    user_id: uuid.UUID = Depends(current_user_id),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """The ranked shortlist, percentile-presented."""
    rows = session.execute(
        text(
            """
            SELECT m.id, m.total_score, m.percentile, m.rank, m.subscores, m.explanation,
                   m.gaps, p.title, p.apply_url, p.remote_type, p.locations, p.posted_at,
                   p.url_status, p.last_verified_at, c.canonical_name AS company
              FROM matches m
              JOIN job_postings p ON p.id = m.posting_id
              LEFT JOIN companies c ON c.id = p.company_id
             WHERE m.user_id = :user_id AND m.gate_passed
               AND m.model_version = :model_version
               AND m.percentile >= :min_percentile
               -- Cast is required: Postgres cannot infer the type of a bare
               -- parameter that is only ever compared with NULL.
               AND (CAST(:remote AS boolean) IS NULL
                    OR (p.remote_type = 'remote') = CAST(:remote AS boolean))
             ORDER BY m.total_score DESC, m.id
             LIMIT :limit OFFSET :cursor
            """
        ),
        {
            "user_id": user_id,
            "model_version": MODEL_VERSION,
            "min_percentile": min_percentile,
            "remote": remote,
            "limit": limit,
            "cursor": cursor,
        },
    ).all()

    pool_size = session.execute(
        text(
            "SELECT count(*) FROM matches WHERE user_id = :user_id AND gate_passed "
            "AND model_version = :model_version"
        ),
        {"user_id": user_id, "model_version": MODEL_VERSION},
    ).scalar_one()

    return {
        "matches": [_card(row, pool_size) for row in rows],
        "pool_size": pool_size,
        "next_cursor": cursor + len(rows) if len(rows) == limit else None,
    }


@router.get("/withheld")
def withheld(
    limit: int = Query(default=50, le=200),
    user_id: uuid.UUID = Depends(current_user_id),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Postings a gate excluded, and why (§8.3).

    Withholding without explanation is indistinguishable from a broken search.
    """
    rows = session.execute(
        text(
            """
            SELECT m.gate_failures, p.title, c.canonical_name AS company
              FROM matches m
              JOIN job_postings p ON p.id = m.posting_id
              LEFT JOIN companies c ON c.id = p.company_id
             WHERE m.user_id = :user_id AND NOT m.gate_passed
               AND m.model_version = :model_version
             ORDER BY m.computed_at DESC
             LIMIT :limit
            """
        ),
        {"user_id": user_id, "model_version": MODEL_VERSION, "limit": limit},
    ).all()

    summary: dict[str, int] = {}
    for row in rows:
        for failure in row.gate_failures or []:
            summary[failure] = summary.get(failure, 0) + 1

    return {
        "withheld": [
            {
                "title": row.title,
                "company": row.company,
                "reasons": [
                    {
                        "code": failure,
                        "explanation": HUMAN_READABLE.get(GateFailure(failure), failure),
                    }
                    for failure in (row.gate_failures or [])
                ],
            }
            for row in rows
        ],
        "summary": summary,
    }


@router.get("/{match_id}")
def match_detail(
    match_id: uuid.UUID,
    user_id: uuid.UUID = Depends(current_user_id),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Full analysis: sub-scores, requirement evidence, gaps, apply URL."""
    row = session.execute(
        text(
            """
            SELECT m.id, m.total_score, m.percentile, m.subscores, m.explanation, m.gaps,
                   m.computed_at, m.model_version, p.id AS posting_id, p.title, p.apply_url,
                   p.description_text, p.remote_type, p.locations, p.posted_at, p.url_status,
                   p.last_verified_at, c.canonical_name AS company, c.careers_url
              FROM matches m
              JOIN job_postings p ON p.id = m.posting_id
              LEFT JOIN companies c ON c.id = p.company_id
             WHERE m.id = :id AND m.user_id = :user_id
            """
        ),
        {"id": match_id, "user_id": user_id},
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Match not found")

    evidence = session.execute(
        text(
            """
            SELECT r.text AS requirement, r.kind, r.is_must_have, e.status, e.similarity,
                   e.note AS matched_bullet
              FROM match_evidence e
              JOIN job_requirements r ON r.id = e.requirement_id
             WHERE e.match_id = :id
             ORDER BY r.is_must_have DESC, e.similarity DESC
            """
        ),
        {"id": match_id},
    ).all()

    pool_size = session.execute(
        text(
            "SELECT count(*) FROM matches WHERE user_id = :user_id AND gate_passed "
            "AND model_version = :model_version"
        ),
        {"user_id": user_id, "model_version": row.model_version},
    ).scalar_one()

    subscores = dict(row.subscores or {})
    contributions = subscores.pop("contributions", {})

    return {
        "match_id": str(row.id),
        "posting_id": str(row.posting_id),
        "title": row.title,
        "company": row.company,
        "locations": row.locations or [],
        "remote_type": row.remote_type,
        "posted_at": row.posted_at,
        # Returned verbatim (§12.7), with the verification facts beside it so a
        # client can show how recently the link was confirmed.
        "apply_url": row.apply_url,
        "url_status": row.url_status,
        "last_verified_at": row.last_verified_at,
        "presentation": _presentation(row.percentile, pool_size),
        "subscores": subscores,
        "contributions": contributions,
        "explanation": row.explanation,
        "gaps": list(row.gaps or []),
        "requirements": [
            {
                "requirement": item.requirement,
                "kind": item.kind,
                "must_have": item.is_must_have,
                "status": item.status,
                "similarity": float(item.similarity) if item.similarity is not None else None,
                "evidence_from_cv": item.matched_bullet,
            }
            for item in evidence
        ],
        "excerpt": (row.description_text or "")[:600],
        "computed_at": row.computed_at,
    }


def _card(row: Any, pool_size: int) -> dict[str, Any]:
    subscores = dict(row.subscores or {})
    subscores.pop("contributions", None)
    return {
        "match_id": str(row.id),
        "title": row.title,
        "company": row.company,
        "locations": row.locations or [],
        "remote_type": row.remote_type,
        "posted_at": row.posted_at,
        "apply_url": row.apply_url,
        "url_status": row.url_status,
        "last_verified_at": row.last_verified_at,
        "presentation": _presentation(row.percentile, pool_size),
        "explanation": row.explanation,
        "gaps": list(row.gaps or []),
        "subscores": subscores,
    }


def _presentation(percentile_value: Any, pool_size: int) -> dict[str, Any]:
    """Percentile only. No absolute score, and never a probability (ADR 0006)."""
    value = float(percentile_value) if percentile_value is not None else 0.0
    percentile = Percentile(value=value, pool_size=pool_size, rank=0)
    return {
        "percentile": value,
        "band": percentile.band,
        "summary": percentile.describe(),
        "pool_size": pool_size,
    }
