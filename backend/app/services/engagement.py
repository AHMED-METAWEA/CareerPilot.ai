"""Saving, dismissing and application tracking — W6 (§11.6, §12.4).

The system never submits an application. "Apply" opens the verified apply URL in
a new tab and records that the user went; the handoff is deliberate and visible
(§1.4).

Two things here earn their complexity:

* **The duplicate-application guard.** A user who applied to a role through one
  board should not be invited to apply again through an aggregator's copy of it.
  The check is on the `job_group`, not the posting, because that is where
  deduplication put the knowledge that they are the same job.
* **The CV snapshot.** An application records which `cv_version` was used, so
  months later the tracker can still say which version of a CV went out — the
  question every applicant eventually asks.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.session import rows_affected

log = structlog.get_logger(__name__)


class EngagementEvent(StrEnum):
    VIEWED = "viewed"
    SAVED = "saved"
    DISMISSED = "dismissed"
    APPLIED = "applied"


class ApplicationStatus(StrEnum):
    APPLIED = "applied"
    SCREENING = "screening"
    INTERVIEWING = "interviewing"
    OFFER = "offer"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    NO_RESPONSE = "no_response"
    """Named explicitly because it is the most common outcome, and a tracker
    that offers no word for it quietly teaches people that silence is failure."""


ALLOWED_TRANSITIONS: dict[ApplicationStatus, frozenset[ApplicationStatus]] = {
    ApplicationStatus.APPLIED: frozenset(
        {
            ApplicationStatus.SCREENING,
            ApplicationStatus.INTERVIEWING,
            ApplicationStatus.REJECTED,
            ApplicationStatus.WITHDRAWN,
            ApplicationStatus.NO_RESPONSE,
        }
    ),
    ApplicationStatus.SCREENING: frozenset(
        {
            ApplicationStatus.INTERVIEWING,
            ApplicationStatus.REJECTED,
            ApplicationStatus.WITHDRAWN,
            ApplicationStatus.NO_RESPONSE,
        }
    ),
    ApplicationStatus.INTERVIEWING: frozenset(
        {
            ApplicationStatus.OFFER,
            ApplicationStatus.REJECTED,
            ApplicationStatus.WITHDRAWN,
            ApplicationStatus.NO_RESPONSE,
        }
    ),
    ApplicationStatus.OFFER: frozenset({ApplicationStatus.REJECTED, ApplicationStatus.WITHDRAWN}),
    ApplicationStatus.REJECTED: frozenset(),
    ApplicationStatus.WITHDRAWN: frozenset(),
    ApplicationStatus.NO_RESPONSE: frozenset(
        {ApplicationStatus.SCREENING, ApplicationStatus.INTERVIEWING, ApplicationStatus.REJECTED}
    ),
}


class EngagementError(Exception):
    pass


class DuplicateApplication(EngagementError):
    """Already applied to this job — possibly through a different board."""

    def __init__(self, message: str, *, existing_id: uuid.UUID, same_group: bool) -> None:
        super().__init__(message)
        self.existing_id = existing_id
        self.same_group = same_group


@dataclass(frozen=True, slots=True)
class ApplicationRecord:
    id: uuid.UUID
    posting_id: uuid.UUID
    status: ApplicationStatus
    cv_version_id: uuid.UUID


class EngagementService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ── implicit signals ─────────────────────────────────────────────

    def record_event(
        self, user_id: uuid.UUID, posting_id: uuid.UUID, event: EngagementEvent
    ) -> uuid.UUID:
        """Record one engagement event.

        These are the dense implicit signal Phase 6 learns from (§11.8), so they
        are appended rather than overwritten: a user who saves, dismisses and
        saves again has told us something a single current-state column would
        throw away.
        """
        self._require_posting(posting_id)
        event_id = self.session.execute(
            text(
                """
                INSERT INTO user_job_events (user_id, posting_id, event)
                VALUES (:user_id, :posting_id, :event)
                RETURNING id
                """
            ),
            {"user_id": user_id, "posting_id": posting_id, "event": event.value},
        ).scalar_one()
        return uuid.UUID(str(event_id))

    def saved_postings(self, user_id: uuid.UUID, *, limit: int = 50) -> list[dict[str, object]]:
        """Currently saved: the latest event for a posting is `saved`."""
        rows = self.session.execute(
            text(
                """
                WITH latest AS (
                    SELECT DISTINCT ON (posting_id) posting_id, event, created_at
                      FROM user_job_events
                     WHERE user_id = :user_id AND event IN ('saved', 'dismissed')
                     ORDER BY posting_id, created_at DESC
                )
                SELECT p.id, p.title, p.apply_url, p.url_status, l.created_at AS saved_at,
                       c.canonical_name AS company
                  FROM latest l
                  JOIN job_postings p ON p.id = l.posting_id
                  LEFT JOIN companies c ON c.id = p.company_id
                 WHERE l.event = 'saved'
                 ORDER BY l.created_at DESC
                 LIMIT :limit
                """
            ),
            {"user_id": user_id, "limit": limit},
        ).all()
        return [dict(row._mapping) for row in rows]

    # ── applications ─────────────────────────────────────────────────

    def record_application(
        self,
        user_id: uuid.UUID,
        posting_id: uuid.UUID,
        *,
        cv_version_id: uuid.UUID | None = None,
        force: bool = False,
    ) -> ApplicationRecord:
        """Record that the user applied. Refuses a duplicate unless forced."""
        job_group_id = self._require_posting(posting_id)

        existing = self._existing_application(user_id, posting_id, job_group_id)
        if existing and not force:
            same_posting = existing.posting_id == posting_id
            raise DuplicateApplication(
                (
                    "You have already applied to this posting."
                    if same_posting
                    else "You have already applied to this job through another board."
                ),
                existing_id=existing.id,
                same_group=not same_posting,
            )

        if cv_version_id is None:
            cv_version_id = self._latest_cv_version(user_id)
        if cv_version_id is None:
            raise EngagementError("Upload a CV before recording an application")

        row = self.session.execute(
            text(
                """
                INSERT INTO applications (user_id, posting_id, cv_version_id, status)
                VALUES (:user_id, :posting_id, :cv_version_id, 'applied')
                ON CONFLICT (user_id, posting_id) DO UPDATE SET applied_at = now()
                RETURNING id, status, cv_version_id
                """
            ),
            {"user_id": user_id, "posting_id": posting_id, "cv_version_id": cv_version_id},
        ).one()

        self.session.execute(
            text(
                """
                INSERT INTO application_events (application_id, from_status, to_status, note)
                VALUES (:application_id, NULL, 'applied', :note)
                """
            ),
            {
                "application_id": row.id,
                "note": "Recorded by the candidate; CareerPilot does not submit applications.",
            },
        )
        self.record_event(user_id, posting_id, EngagementEvent.APPLIED)

        log.info("application.recorded", user_id=str(user_id), posting_id=str(posting_id))
        return ApplicationRecord(
            id=uuid.UUID(str(row.id)),
            posting_id=posting_id,
            status=ApplicationStatus(row.status),
            cv_version_id=uuid.UUID(str(row.cv_version_id)),
        )

    def advance(
        self,
        user_id: uuid.UUID,
        application_id: uuid.UUID,
        to_status: ApplicationStatus,
        *,
        note: str | None = None,
    ) -> ApplicationRecord:
        """Move an application along, writing an event for the transition."""
        row = self.session.execute(
            text(
                "SELECT id, posting_id, status, cv_version_id FROM applications "
                "WHERE id = :id AND user_id = :user_id"
            ),
            {"id": application_id, "user_id": user_id},
        ).first()
        if row is None:
            raise EngagementError("Application not found")

        current = ApplicationStatus(row.status)
        if to_status == current:
            return ApplicationRecord(
                id=application_id,
                posting_id=uuid.UUID(str(row.posting_id)),
                status=current,
                cv_version_id=uuid.UUID(str(row.cv_version_id)),
            )
        if to_status not in ALLOWED_TRANSITIONS[current]:
            raise EngagementError(
                f"Cannot move an application from {current.value} to {to_status.value}"
            )

        self.session.execute(
            text("UPDATE applications SET status = :status WHERE id = :id"),
            {"status": to_status.value, "id": application_id},
        )
        self.session.execute(
            text(
                """
                INSERT INTO application_events (application_id, from_status, to_status, note)
                VALUES (:application_id, :from_status, :to_status, :note)
                """
            ),
            {
                "application_id": application_id,
                "from_status": current.value,
                "to_status": to_status.value,
                "note": note,
            },
        )
        return ApplicationRecord(
            id=application_id,
            posting_id=uuid.UUID(str(row.posting_id)),
            status=to_status,
            cv_version_id=uuid.UUID(str(row.cv_version_id)),
        )

    def list_applications(self, user_id: uuid.UUID) -> list[dict[str, object]]:
        rows = self.session.execute(
            text(
                """
                SELECT a.id, a.status, a.applied_at, a.cv_version_id,
                       p.id AS posting_id, p.title, p.apply_url, p.url_status,
                       c.canonical_name AS company,
                       (SELECT count(*) FROM application_events e
                         WHERE e.application_id = a.id) AS event_count
                  FROM applications a
                  JOIN job_postings p ON p.id = a.posting_id
                  LEFT JOIN companies c ON c.id = p.company_id
                 WHERE a.user_id = :user_id
                 ORDER BY a.applied_at DESC
                """
            ),
            {"user_id": user_id},
        ).all()
        return [dict(row._mapping) for row in rows]

    def application_history(
        self, user_id: uuid.UUID, application_id: uuid.UUID
    ) -> list[dict[str, object]]:
        rows = self.session.execute(
            text(
                """
                SELECT e.from_status, e.to_status, e.occurred_at, e.note
                  FROM application_events e
                  JOIN applications a ON a.id = e.application_id
                 WHERE e.application_id = :id AND a.user_id = :user_id
                 ORDER BY e.occurred_at
                """
            ),
            {"id": application_id, "user_id": user_id},
        ).all()
        return [dict(row._mapping) for row in rows]

    def counters(self, user_id: uuid.UUID) -> dict[str, int]:
        """The three counters the profile page shows (§3.2)."""
        row = self.session.execute(
            text(
                """
                SELECT
                  (SELECT count(*) FROM applications WHERE user_id = :user_id) AS applications,
                  (SELECT count(*) FROM (
                     SELECT DISTINCT ON (posting_id) event FROM user_job_events
                      WHERE user_id = :user_id AND event IN ('saved', 'dismissed')
                      ORDER BY posting_id, created_at DESC
                   ) AS latest WHERE event = 'saved') AS saved,
                  (SELECT count(*) FROM matches
                    WHERE user_id = :user_id AND gate_passed) AS matches
                """
            ),
            {"user_id": user_id},
        ).one()
        return {"applications": row.applications, "saved": row.saved, "matches": row.matches}

    # ── helpers ──────────────────────────────────────────────────────

    def _require_posting(self, posting_id: uuid.UUID) -> uuid.UUID | None:
        """Assert the posting exists, and return the job group it belongs to."""
        row = self.session.execute(
            text("SELECT job_group_id FROM job_postings WHERE id = :id"), {"id": posting_id}
        ).first()
        if row is None:
            raise EngagementError("Posting not found")
        return uuid.UUID(str(row.job_group_id)) if row.job_group_id else None

    def _existing_application(
        self, user_id: uuid.UUID, posting_id: uuid.UUID, job_group_id: uuid.UUID | None
    ) -> ApplicationRecord | None:
        """An application to this posting, or to any posting in its group.

        The group is the point: an aggregator's copy of a role the user already
        applied to is the same application, and inviting them to send a second
        one is exactly the reputational damage §1.2 is about.
        """
        row = self.session.execute(
            text(
                """
                SELECT a.id, a.posting_id, a.status, a.cv_version_id
                  FROM applications a
                  JOIN job_postings p ON p.id = a.posting_id
                 WHERE a.user_id = :user_id
                   AND (a.posting_id = :posting_id
                        OR (CAST(:job_group_id AS uuid) IS NOT NULL
                            AND p.job_group_id = CAST(:job_group_id AS uuid)))
                 ORDER BY a.applied_at DESC
                 LIMIT 1
                """
            ),
            {"user_id": user_id, "posting_id": posting_id, "job_group_id": job_group_id},
        ).first()
        if row is None:
            return None
        return ApplicationRecord(
            id=uuid.UUID(str(row.id)),
            posting_id=uuid.UUID(str(row.posting_id)),
            status=ApplicationStatus(row.status),
            cv_version_id=uuid.UUID(str(row.cv_version_id)),
        )

    def _latest_cv_version(self, user_id: uuid.UUID) -> uuid.UUID | None:
        row = self.session.execute(
            text(
                """
                SELECT v.id FROM cv_versions v
                  JOIN cv_documents d ON d.id = v.cv_document_id
                 WHERE d.user_id = :user_id
                 ORDER BY v.created_at DESC LIMIT 1
                """
            ),
            {"user_id": user_id},
        ).first()
        return uuid.UUID(str(row.id)) if row else None

    def delete_events(self, user_id: uuid.UUID) -> int:
        """Used by account deletion (§12.5)."""
        return rows_affected(
            self.session.execute(
                text("DELETE FROM user_job_events WHERE user_id = :user_id"),
                {"user_id": user_id},
            )
        )
