"""The daily digest (§11.7).

What makes it a digest rather than a mailing: consent checked at send time,
new means new, nothing sent when there is nothing to say.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.services.auth import AuthService
from app.services.digest import DigestService, LoggingSender
from app.services.matching import MODEL_VERSION

pytestmark = pytest.mark.db


@pytest.fixture
def sender() -> LoggingSender:
    return LoggingSender()


@pytest.fixture
def service(db_session: Session, sender: LoggingSender) -> DigestService:
    return DigestService(db_session, sender)


@pytest.fixture
def user_with_matches(db_session: Session) -> uuid.UUID:
    auth = AuthService(db_session, secret="test-secret")
    user = auth.register(
        f"{uuid.uuid4().hex[:10]}@example.com",
        "a-long-enough-passphrase",
        consents=("processing", "digest"),
    )

    document_id = db_session.execute(
        sa.text(
            "INSERT INTO cv_documents (user_id, storage_key, mime, sha256) "
            "VALUES (:u, 'k', 'text/plain', :s) RETURNING id"
        ),
        {"u": user.user_id, "s": uuid.uuid4().hex * 2},
    ).scalar_one()
    cv_version_id = db_session.execute(
        sa.text(
            "INSERT INTO cv_versions (cv_document_id, version, raw_text, parse_quality, "
            "parser_version) VALUES (:d, 1, 'cv', 0.9, 't') RETURNING id"
        ),
        {"d": document_id},
    ).scalar_one()
    profile_id = db_session.execute(
        sa.text(
            "INSERT INTO candidate_profiles (user_id, cv_version_id) VALUES (:u, :v) RETURNING id"
        ),
        {"u": user.user_id, "v": cv_version_id},
    ).scalar_one()

    source_id = db_session.execute(
        sa.text(
            "INSERT INTO job_sources (adapter, name, config) "
            "VALUES ('greenhouse', :n, '{}'::jsonb) RETURNING id"
        ),
        {"n": f"greenhouse:{uuid.uuid4().hex[:6]}"},
    ).scalar_one()

    for index, (percentile, url_status) in enumerate(
        [(98.0, "live"), (90.0, "live"), (50.0, "live"), (99.0, "unknown")]
    ):
        posting_id = db_session.execute(
            sa.text(
                """
                INSERT INTO job_postings (source_id, external_id, title, title_normalized,
                    description_text, apply_url, source_url, url_status, status)
                VALUES (:s, :e, :t, lower(:t), 'body', :url, :url, :url_status, 'open')
                RETURNING id
                """
            ),
            {
                "s": source_id,
                "e": str(index),
                "t": f"Data Engineer {index}",
                "url": f"https://boards.greenhouse.io/acme/jobs/{index}",
                "url_status": url_status,
            },
        ).scalar_one()
        db_session.execute(
            sa.text(
                """
                INSERT INTO matches (user_id, profile_id, posting_id, total_score, percentile,
                                     gate_passed, subscores, model_version, explanation, gaps)
                VALUES (:u, :p, :j, :score, :percentile, true, '{}'::jsonb, :version,
                        'Matches 4/6 of the listed skill requirements. Seniority: level matches.',
                        ARRAY['Kubernetes'])
                """
            ),
            {
                "u": user.user_id,
                "p": profile_id,
                "j": posting_id,
                "score": percentile / 100,
                "percentile": percentile,
                "version": MODEL_VERSION,
            },
        )
    db_session.commit()
    return user.user_id


def test_digest_takes_only_matches_above_the_threshold(
    service: DigestService, user_with_matches: uuid.UUID
) -> None:
    digest = service.compose(user_with_matches, min_percentile=75.0)
    assert len(digest.items) == 2, "the 50th-percentile match is below the threshold"


def test_unverified_postings_are_never_sent(
    service: DigestService, user_with_matches: uuid.UUID
) -> None:
    """A digest is the most public promise the product makes; a dead link in one
    is worse than no digest."""
    digest = service.compose(user_with_matches, min_percentile=75.0)
    assert all("jobs/3" not in item.apply_url for item in digest.items)


def test_the_digest_is_capped(db_session: Session, service: DigestService) -> None:
    from app.services.digest import MAX_ITEMS

    assert MAX_ITEMS == 5, "five is a shortlist; twenty is a job board"


def test_sending_records_what_was_sent_and_does_not_repeat_it(
    db_session: Session, service: DigestService, sender: LoggingSender, user_with_matches: uuid.UUID
) -> None:
    """A digest that repeats yesterday's list trains people to stop opening it."""
    first = service.send(user_with_matches)
    db_session.commit()
    assert first["sent"] is True and first["items"] == 2
    assert len(sender.sent) == 1

    second = service.send(user_with_matches)
    db_session.commit()
    assert second["sent"] is False
    assert second["reason"] == "nothing new above the threshold"
    assert len(sender.sent) == 1


def test_nothing_is_sent_without_consent(
    db_session: Session, service: DigestService, sender: LoggingSender, user_with_matches: uuid.UUID
) -> None:
    """Consent is checked at send time, so an unsubscribe five minutes ago counts."""
    AuthService(db_session, secret="test-secret").revoke_consent(user_with_matches, "digest")
    db_session.commit()

    result = service.send(user_with_matches)
    assert result["sent"] is False and result["reason"] == "no digest consent"
    assert sender.sent == []


def test_an_empty_digest_is_not_sent(db_session: Session, service: DigestService) -> None:
    user = AuthService(db_session, secret="test-secret").register(
        f"{uuid.uuid4().hex[:10]}@example.com",
        "a-long-enough-passphrase",
        consents=("processing", "digest"),
    )
    db_session.commit()
    assert service.send(user.user_id)["sent"] is False


def test_the_body_says_what_the_product_does_and_does_not_do(
    service: DigestService, user_with_matches: uuid.UUID
) -> None:
    digest = service.compose(user_with_matches)
    body = digest.render_text()

    assert "never applies on your behalf" in body
    assert "Stop receiving this" in body, "unsubscribe is part of the message (§16.4)"
    for item in digest.items:
        assert item.apply_url in body


def test_the_digest_never_implies_a_probability(
    service: DigestService, user_with_matches: uuid.UUID
) -> None:
    """ADR 0006, enforced in the send path rather than trusted to copywriting."""
    from app.domain.scoring.percentile import violates_presentation_rules

    body = service.compose(user_with_matches).render_text()
    assert violates_presentation_rules(body) == []


def test_reasons_come_from_the_stored_explanation(
    service: DigestService, user_with_matches: uuid.UUID
) -> None:
    """§10.4: the digest says the same thing the match detail says."""
    digest = service.compose(user_with_matches)
    assert digest.items[0].reason.startswith("Matches 4/6")
    assert "Kubernetes" in digest.items[0].reason


def test_due_users_excludes_the_recently_sent(
    db_session: Session, service: DigestService, user_with_matches: uuid.UUID
) -> None:
    assert user_with_matches in service.due_users()
    service.send(user_with_matches)
    db_session.commit()
    assert user_with_matches not in service.due_users()


def test_a_deleted_user_gets_nothing(
    db_session: Session, service: DigestService, user_with_matches: uuid.UUID
) -> None:
    db_session.execute(
        sa.text("UPDATE users SET deleted_at = now() WHERE id = :id"), {"id": user_with_matches}
    )
    db_session.commit()

    assert user_with_matches not in service.due_users()
    with pytest.raises(ValueError, match="not found or deleted"):
        service.compose(user_with_matches)
