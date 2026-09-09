"""The daily digest — W7 (§11.7).

New matches above the user's threshold, capped at five, one line of reason each.
One scheduled email, not a notification service (§3.2).

The rules that make it a digest rather than a mailing:

* **Consent is checked at send time**, not at compose time. Unsubscribing is
  honoured immediately (§11.7), which means a queued digest for someone who
  revoked consent five minutes ago does not go out.
* **New means new.** A match already sent is not sent again; a digest that
  repeats yesterday's list trains people to stop opening it.
* **Nothing is sent when there is nothing to say.** An empty digest is worse
  than no digest.
* **Percentile phrasing only** (§8.5). The reason line never describes a score
  as a chance of anything.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.domain.scoring.percentile import violates_presentation_rules

log = structlog.get_logger(__name__)

MAX_ITEMS = 5
"""§11.7. Five is a shortlist; twenty is a job board."""
DEFAULT_MIN_PERCENTILE = 75.0


@dataclass(frozen=True, slots=True)
class DigestItem:
    match_id: uuid.UUID
    posting_id: uuid.UUID
    title: str
    company: str | None
    apply_url: str
    percentile: float
    reason: str


@dataclass(slots=True)
class Digest:
    user_id: uuid.UUID
    email: str
    items: list[DigestItem] = field(default_factory=list)
    pool_size: int = 0

    @property
    def is_worth_sending(self) -> bool:
        return bool(self.items)

    def subject(self) -> str:
        if len(self.items) == 1:
            return f"1 role worth your time: {self.items[0].title}"
        return f"{len(self.items)} roles worth your time this week"

    def render_text(self) -> str:
        """Plain text, because a digest is read on a phone in a queue."""
        lines = [
            f"{len(self.items)} new match{'es' if len(self.items) != 1 else ''} "
            f"from the {self.pool_size:,} roles reviewed for you.",
            "",
        ]
        for index, item in enumerate(self.items, start=1):
            company = f" at {item.company}" if item.company else ""
            lines.append(f"{index}. {item.title}{company}")
            lines.append(f"   {item.reason}")
            lines.append(f"   {item.apply_url}")
            lines.append("")
        lines.append("CareerPilot never applies on your behalf; these links open the employer's")
        lines.append("own application page.")
        lines.append("")
        lines.append(
            'Stop receiving this: PATCH /api/v1/me/consents {"purpose": "digest", "granted": false}'
        )
        return "\n".join(lines)


class EmailSender(Protocol):
    """What a delivery provider must implement."""

    name: str

    def send(self, *, to: str, subject: str, body: str) -> str: ...


class LoggingSender:
    """Development sender: records the message instead of delivering it.

    Nothing is sent until a provider is configured, which is the correct
    behaviour for a system that has not yet completed the §16.4 checkpoint on
    outbound email — unsubscribe mechanism, sender identification, consent
    record — for a real domain.
    """

    name = "logging"

    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    def send(self, *, to: str, subject: str, body: str) -> str:
        self.sent.append({"to": to, "subject": subject, "body": body})
        log.info(
            "digest.not_delivered", to=to, subject=subject, reason="no email provider configured"
        )
        return f"logged:{len(self.sent)}"


class DigestService:
    def __init__(self, session: Session, sender: EmailSender | None = None) -> None:
        self.session = session
        self.sender = sender or LoggingSender()

    def compose(
        self, user_id: uuid.UUID, *, min_percentile: float = DEFAULT_MIN_PERCENTILE
    ) -> Digest:
        """Build a digest of matches this user has not been sent before."""
        user = self.session.execute(
            text("SELECT email FROM users WHERE id = :id AND deleted_at IS NULL"),
            {"id": user_id},
        ).first()
        if user is None:
            raise ValueError("user not found or deleted")

        rows = self.session.execute(
            text(
                """
                SELECT m.id, m.posting_id, m.percentile, m.explanation, m.gaps,
                       p.title, p.apply_url, c.canonical_name AS company
                  FROM matches m
                  JOIN job_postings p ON p.id = m.posting_id
                  LEFT JOIN companies c ON c.id = p.company_id
                 WHERE m.user_id = :user_id
                   AND m.gate_passed
                   AND m.percentile >= :min_percentile
                   AND p.status = 'open'
                   AND p.url_status = 'live'
                   AND NOT EXISTS (
                         SELECT 1 FROM digest_sends d
                          WHERE d.user_id = m.user_id AND d.posting_id = m.posting_id
                       )
                 ORDER BY m.total_score DESC
                 LIMIT :limit
                """
            ),
            {"user_id": user_id, "min_percentile": min_percentile, "limit": MAX_ITEMS},
        ).all()

        pool_size = self.session.execute(
            text("SELECT count(*) FROM matches WHERE user_id = :id AND gate_passed"),
            {"id": user_id},
        ).scalar_one()

        digest = Digest(user_id=user_id, email=user.email, pool_size=pool_size)
        for row in rows:
            digest.items.append(
                DigestItem(
                    match_id=uuid.UUID(str(row.id)),
                    posting_id=uuid.UUID(str(row.posting_id)),
                    title=row.title,
                    company=row.company,
                    apply_url=row.apply_url,
                    percentile=float(row.percentile or 0),
                    reason=_reason(row),
                )
            )
        return digest

    def send(
        self, user_id: uuid.UUID, *, min_percentile: float = DEFAULT_MIN_PERCENTILE
    ) -> dict[str, Any]:
        """Compose and deliver, if consent still holds and there is anything to say."""
        if not self._has_digest_consent(user_id):
            return {"sent": False, "reason": "no digest consent"}

        digest = self.compose(user_id, min_percentile=min_percentile)
        if not digest.is_worth_sending:
            return {"sent": False, "reason": "nothing new above the threshold"}

        body = digest.render_text()
        violations = violates_presentation_rules(body)
        if violations:
            # A guard, not a formality: generated copy that reads as a hire
            # probability must not reach a user (§8.5, ADR 0006).
            log.error("digest.presentation_violation", phrases=violations)
            return {"sent": False, "reason": f"presentation rule violation: {violations}"}

        message_id = self.sender.send(to=digest.email, subject=digest.subject(), body=body)
        self._record(digest, message_id)

        log.info(
            "digest.sent",
            user_id=str(user_id),
            items=len(digest.items),
            provider=self.sender.name,
        )
        return {
            "sent": True,
            "items": len(digest.items),
            "message_id": message_id,
            "provider": self.sender.name,
        }

    def _has_digest_consent(self, user_id: uuid.UUID) -> bool:
        """Checked at send time: an unsubscribe five minutes ago still counts."""
        return (
            self.session.execute(
                text(
                    """
                    SELECT 1 FROM consents
                     WHERE user_id = :id AND purpose = 'digest' AND revoked_at IS NULL
                     LIMIT 1
                    """
                ),
                {"id": user_id},
            ).first()
            is not None
        )

    def _record(self, digest: Digest, message_id: str) -> None:
        for item in digest.items:
            self.session.execute(
                text(
                    """
                    INSERT INTO digest_sends (user_id, posting_id, match_id, message_id, sent_at)
                    VALUES (:user_id, :posting_id, :match_id, :message_id, now())
                    ON CONFLICT (user_id, posting_id) DO NOTHING
                    """
                ),
                {
                    "user_id": digest.user_id,
                    "posting_id": item.posting_id,
                    "match_id": item.match_id,
                    "message_id": message_id,
                },
            )

    def due_users(self, *, hour_utc: int | None = None) -> list[uuid.UUID]:
        """Users with digest consent who have not been sent one today."""
        del hour_utc  # per-user local-time scheduling arrives with the timezone column
        rows = (
            self.session.execute(
                text(
                    """
                    SELECT DISTINCT u.id
                      FROM users u
                      JOIN consents c ON c.user_id = u.id
                     WHERE u.deleted_at IS NULL
                       AND c.purpose = 'digest' AND c.revoked_at IS NULL
                       AND NOT EXISTS (
                             SELECT 1 FROM digest_sends d
                              WHERE d.user_id = u.id AND d.sent_at > now() - interval '20 hours'
                           )
                    """
                )
            )
            .scalars()
            .all()
        )
        return [uuid.UUID(str(row)) for row in rows]


def _reason(row: Any) -> str:
    """One line, drawn from the stored explanation and gaps.

    Assembled from facts already computed, not generated afresh: the digest must
    say the same thing the match detail says (§10.4).
    """
    explanation = (row.explanation or "").strip()
    first_sentence = explanation.split(". ")[0] if explanation else ""
    gaps = list(row.gaps or [])
    if gaps:
        return f"{first_sentence}. Gap: {gaps[0]}." if first_sentence else f"Gap: {gaps[0]}."
    return first_sentence or f"In the top {100 - float(row.percentile or 0):.0f}% of your matches."


def utcnow() -> datetime:
    return datetime.now(UTC)
