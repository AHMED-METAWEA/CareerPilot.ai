"""Data export, erasure and the audit trail (§12.5, §16.2, §16.3).

The GDPR rights the plan commits to, implemented rather than promised: access
(`export`), rectification (handled by `PATCH /profile`) and erasure (`delete`).

Erasure is two-stage on purpose, and the stages mean different things:

* **Immediately**, on request: the account is closed, every session is revoked,
  and the CV text — the sensitive-personal class in §16.1 — is destroyed. The
  user stops being processed the moment they ask.
* **After the retention window**, the remaining rows are dropped. The window
  exists so a deletion made in error or under duress can be reversed, and so
  the audit trail of the deletion itself survives the deletion.

Every access, export and deletion writes to `audit_log` (§16.2). The audit row
outlives the data it describes, which is the point of having one.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.session import rows_affected

log = structlog.get_logger(__name__)

CV_TEXT_RETENTION = timedelta(days=30)
"""§6.4: CV text is purged 30 days after a deletion request, and the purge is
recorded. Held that long only so an accidental deletion can be undone."""


@dataclass(slots=True)
class DeletionReceipt:
    user_id: uuid.UUID
    requested_at: datetime
    cv_text_purged: int
    sessions_revoked: int
    hard_delete_after: datetime


class AccountService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ── audit ────────────────────────────────────────────────────────

    def audit(
        self,
        *,
        user_id: uuid.UUID | None,
        actor: str,
        action: str,
        subject: str | None = None,
        detail: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> None:
        self.session.execute(
            text(
                """
                INSERT INTO audit_log (user_id, actor, action, subject, detail, request_id)
                VALUES (:user_id, :actor, :action, :subject, CAST(:detail AS jsonb), :request_id)
                """
            ),
            {
                "user_id": user_id,
                "actor": actor,
                "action": action,
                "subject": subject,
                "detail": json.dumps(detail or {}, default=str),
                "request_id": request_id,
            },
        )

    # ── export (§12.5) ───────────────────────────────────────────────

    def export(self, user_id: uuid.UUID, *, request_id: str | None = None) -> dict[str, Any]:
        """Everything held about one user, in one document.

        Portable and readable: a JSON export a person can open, not a database
        dump they need us to interpret for them.
        """
        user = self.session.execute(
            text("SELECT id, email, locale, created_at FROM users WHERE id = :id"),
            {"id": user_id},
        ).one()

        export: dict[str, Any] = {
            "exported_at": datetime.now(UTC).isoformat(),
            "account": {
                "user_id": str(user.id),
                "email": user.email,
                "locale": user.locale,
                "created_at": user.created_at,
            },
            "consents": self._rows(
                "SELECT purpose, policy_version, granted_at, revoked_at FROM consents "
                "WHERE user_id = :id ORDER BY granted_at",
                user_id,
            ),
            "cv_documents": self._rows(
                "SELECT id, mime, sha256, uploaded_at FROM cv_documents "
                "WHERE user_id = :id ORDER BY uploaded_at",
                user_id,
            ),
            "cv_versions": self._rows(
                """
                SELECT v.id, v.version, v.parse_quality, v.parser_version, v.created_at,
                       v.raw_text
                  FROM cv_versions v JOIN cv_documents d ON d.id = v.cv_document_id
                 WHERE d.user_id = :id ORDER BY v.created_at
                """,
                user_id,
            ),
            "profiles": self._rows(
                "SELECT id, years_experience, seniority_level, locations, work_auth, languages, "
                "summary, extraction_confidence, extracted_at FROM candidate_profiles "
                "WHERE user_id = :id ORDER BY extracted_at",
                user_id,
            ),
            "skills": self._rows(
                """
                SELECT s.canonical_name, ps.years, ps.proficiency, ps.source
                  FROM profile_skills ps
                  JOIN candidate_profiles p ON p.id = ps.profile_id
                  JOIN skills s ON s.id = ps.skill_id
                 WHERE p.user_id = :id ORDER BY s.canonical_name
                """,
                user_id,
            ),
            "applications": self._rows(
                """
                SELECT a.id, a.status, a.applied_at, p.title, p.apply_url,
                       c.canonical_name AS company
                  FROM applications a
                  JOIN job_postings p ON p.id = a.posting_id
                  LEFT JOIN companies c ON c.id = p.company_id
                 WHERE a.user_id = :id ORDER BY a.applied_at
                """,
                user_id,
            ),
            "engagement_events": self._rows(
                "SELECT posting_id, event, created_at FROM user_job_events "
                "WHERE user_id = :id ORDER BY created_at",
                user_id,
            ),
            "matches": self._rows(
                """
                SELECT m.percentile, m.gate_passed, m.gate_failures, m.explanation, m.gaps,
                       m.computed_at, p.title, c.canonical_name AS company
                  FROM matches m
                  JOIN job_postings p ON p.id = m.posting_id
                  LEFT JOIN companies c ON c.id = p.company_id
                 WHERE m.user_id = :id AND m.gate_passed
                 ORDER BY m.computed_at DESC LIMIT 500
                """,
                user_id,
            ),
        }

        # Job descriptions are third-party copyrighted text and are not
        # republished in an export (§16.3); the title and the link are.
        self.audit(
            user_id=user_id,
            actor=str(user_id),
            action="export",
            subject="account",
            detail={"sections": sorted(export)},
            request_id=request_id,
        )
        return export

    # ── erasure (§12.5) ──────────────────────────────────────────────

    def request_deletion(
        self, user_id: uuid.UUID, *, request_id: str | None = None
    ) -> DeletionReceipt:
        """Close the account now; drop the rest after the retention window."""
        now = datetime.now(UTC)

        self.session.execute(
            text("UPDATE users SET deleted_at = now() WHERE id = :id AND deleted_at IS NULL"),
            {"id": user_id},
        )
        sessions = rows_affected(
            self.session.execute(
                text(
                    "UPDATE refresh_tokens SET revoked_at = now() "
                    "WHERE user_id = :id AND revoked_at IS NULL"
                ),
                {"id": user_id},
            )
        )
        # The sensitive-personal class goes immediately: raw CV text is the most
        # identifying thing here, and keeping it for a month "just in case" is
        # not something a deletion request contemplates.
        purged = rows_affected(
            self.session.execute(
                text(
                    """
                    UPDATE cv_versions SET raw_text = ''
                     WHERE cv_document_id IN (SELECT id FROM cv_documents WHERE user_id = :id)
                       AND raw_text <> ''
                    """
                ),
                {"id": user_id},
            )
        )
        self.session.execute(
            text(
                "UPDATE consents SET revoked_at = now() WHERE user_id = :id AND revoked_at IS NULL"
            ),
            {"id": user_id},
        )

        receipt = DeletionReceipt(
            user_id=user_id,
            requested_at=now,
            cv_text_purged=purged,
            sessions_revoked=sessions,
            hard_delete_after=now + CV_TEXT_RETENTION,
        )
        self.audit(
            user_id=user_id,
            actor=str(user_id),
            action="deletion_requested",
            subject="account",
            detail={
                "cv_text_purged": purged,
                "sessions_revoked": sessions,
                "hard_delete_after": receipt.hard_delete_after.isoformat(),
            },
            request_id=request_id,
        )
        log.info("account.deletion_requested", user_id=str(user_id), cv_versions_purged=purged)
        return receipt

    def hard_delete_expired(self, *, now: datetime | None = None) -> int:
        """Drop accounts whose retention window has passed.

        The `audit_log` row survives: it records that a deletion happened and
        when, which is what makes the deletion demonstrable afterwards. It holds
        no CV text and no profile content.
        """
        now = now or datetime.now(UTC)
        cutoff = now - CV_TEXT_RETENTION

        rows = (
            self.session.execute(
                text("SELECT id FROM users WHERE deleted_at IS NOT NULL AND deleted_at < :cutoff"),
                {"cutoff": cutoff},
            )
            .scalars()
            .all()
        )
        for user_id in rows:
            # Everything else cascades from users (§6.2).
            self.session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
            self.audit(
                user_id=None,
                actor="system",
                action="hard_deleted",
                subject=str(user_id),
                detail={"retention_days": CV_TEXT_RETENTION.days},
            )
        if rows:
            log.info("account.hard_deleted", count=len(rows))
        return len(rows)

    def _rows(self, query: str, user_id: uuid.UUID) -> list[dict[str, Any]]:
        return [
            dict(row._mapping) for row in self.session.execute(text(query), {"id": user_id}).all()
        ]
