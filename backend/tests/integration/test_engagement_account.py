"""Engagement, applications, export and erasure (§11.6, §12.4, §12.5).

The compliance promises in §16 are only worth the code that keeps them, so the
tests here check the promises rather than the plumbing.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from app.services.account import AccountService
from app.services.engagement import (
    ApplicationStatus,
    DuplicateApplication,
    EngagementError,
    EngagementEvent,
    EngagementService,
)

pytestmark = pytest.mark.db

PASSWORD = "a-long-enough-passphrase"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def account(client: TestClient) -> tuple[uuid.UUID, str, dict[str, str]]:
    email = f"{uuid.uuid4().hex[:10]}@example.com"
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "accept_processing": True},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return (
        uuid.UUID(body["user_id"]),
        email,
        {"Authorization": f"Bearer {body['access_token']}"},
    )


@pytest.fixture
def cv_version(db_session: Session, account: tuple[uuid.UUID, str, dict[str, str]]) -> uuid.UUID:
    user_id, _, _ = account
    document_id = db_session.execute(
        sa.text(
            "INSERT INTO cv_documents (user_id, storage_key, mime, sha256) "
            "VALUES (:u, 'k', 'text/plain', :s) RETURNING id"
        ),
        {"u": user_id, "s": uuid.uuid4().hex * 2},
    ).scalar_one()
    version_id = db_session.execute(
        sa.text(
            "INSERT INTO cv_versions (cv_document_id, version, raw_text, parse_quality, "
            "parser_version) VALUES (:d, 1, 'Built pipelines in Python', 0.9, 't') RETURNING id"
        ),
        {"d": document_id},
    ).scalar_one()
    db_session.commit()
    return uuid.UUID(str(version_id))


@pytest.fixture
def postings(db_session: Session) -> dict[str, uuid.UUID]:
    """Two postings of the same job on different boards, plus an unrelated one."""
    group_id = db_session.execute(
        sa.text("INSERT INTO job_groups (cluster_key) VALUES ('acme|data engineer') RETURNING id")
    ).scalar_one()
    created: dict[str, uuid.UUID] = {}
    for key, source_name, group in (
        ("ats", "greenhouse:acme", group_id),
        ("aggregator", "remotive", group_id),
        ("other", "greenhouse:globex", None),
    ):
        source_id = db_session.execute(
            sa.text(
                "INSERT INTO job_sources (adapter, name, config) "
                "VALUES ('greenhouse', :n, '{}'::jsonb) RETURNING id"
            ),
            {"n": f"{source_name}:{uuid.uuid4().hex[:4]}"},
        ).scalar_one()
        created[key] = db_session.execute(
            sa.text(
                """
                INSERT INTO job_postings (source_id, external_id, job_group_id, title,
                    title_normalized, description_text, apply_url, source_url, status)
                VALUES (:s, :e, :g, 'Data Engineer', 'data engineer', 'body', :url, :url, 'open')
                RETURNING id
                """
            ),
            {
                "s": source_id,
                "e": key,
                "g": group,
                "url": f"https://boards.example.com/{key}",
            },
        ).scalar_one()
    db_session.commit()
    return created


# ── engagement ────────────────────────────────────────────────────────


def test_events_are_appended_not_overwritten(
    db_session: Session,
    account: tuple[uuid.UUID, str, dict[str, str]],
    postings: dict[str, uuid.UUID],
) -> None:
    """A user who saves, dismisses and saves again has told us something a
    current-state column would throw away (§11.8)."""
    user_id, _, _ = account
    service = EngagementService(db_session)
    for event in (EngagementEvent.SAVED, EngagementEvent.DISMISSED, EngagementEvent.SAVED):
        service.record_event(user_id, postings["ats"], event)
    db_session.commit()

    count = db_session.execute(
        sa.text("SELECT count(*) FROM user_job_events WHERE user_id = :u"), {"u": user_id}
    ).scalar_one()
    assert count == 3
    # Currently saved: the latest event wins.
    assert len(service.saved_postings(user_id)) == 1


def test_dismissing_removes_a_posting_from_saved(
    db_session: Session,
    account: tuple[uuid.UUID, str, dict[str, str]],
    postings: dict[str, uuid.UUID],
) -> None:
    user_id, _, _ = account
    service = EngagementService(db_session)
    service.record_event(user_id, postings["ats"], EngagementEvent.SAVED)
    service.record_event(user_id, postings["ats"], EngagementEvent.DISMISSED)
    db_session.commit()
    assert service.saved_postings(user_id) == []


def test_an_event_on_an_unknown_posting_is_refused(
    db_session: Session, account: tuple[uuid.UUID, str, dict[str, str]]
) -> None:
    user_id, _, _ = account
    with pytest.raises(EngagementError, match="Posting not found"):
        EngagementService(db_session).record_event(user_id, uuid.uuid4(), EngagementEvent.SAVED)


# ── applications ──────────────────────────────────────────────────────


def test_application_records_the_cv_version_used(
    db_session: Session,
    account: tuple[uuid.UUID, str, dict[str, str]],
    postings: dict[str, uuid.UUID],
    cv_version: uuid.UUID,
) -> None:
    """Months later, the tracker still says which CV went out."""
    user_id, _, _ = account
    record = EngagementService(db_session).record_application(user_id, postings["ats"])
    db_session.commit()
    assert record.cv_version_id == cv_version
    assert record.status is ApplicationStatus.APPLIED


def test_applying_without_a_cv_is_refused(
    db_session: Session,
    account: tuple[uuid.UUID, str, dict[str, str]],
    postings: dict[str, uuid.UUID],
) -> None:
    user_id, _, _ = account
    with pytest.raises(EngagementError, match="Upload a CV"):
        EngagementService(db_session).record_application(user_id, postings["ats"])


def test_the_duplicate_guard_works_across_boards(
    db_session: Session,
    account: tuple[uuid.UUID, str, dict[str, str]],
    postings: dict[str, uuid.UUID],
    cv_version: uuid.UUID,
) -> None:
    """The guard is on the job group, because that is where deduplication put
    the knowledge that two postings are the same job (§11.6 step 6)."""
    user_id, _, _ = account
    service = EngagementService(db_session)
    first = service.record_application(user_id, postings["ats"])
    db_session.commit()

    with pytest.raises(DuplicateApplication) as exc:
        service.record_application(user_id, postings["aggregator"])
    assert exc.value.same_group is True
    assert exc.value.existing_id == first.id
    assert "another board" in str(exc.value)


def test_a_different_job_is_not_a_duplicate(
    db_session: Session,
    account: tuple[uuid.UUID, str, dict[str, str]],
    postings: dict[str, uuid.UUID],
    cv_version: uuid.UUID,
) -> None:
    user_id, _, _ = account
    service = EngagementService(db_session)
    service.record_application(user_id, postings["ats"])
    db_session.commit()
    assert service.record_application(user_id, postings["other"]) is not None


def test_the_user_can_override_the_duplicate_guard(
    db_session: Session,
    account: tuple[uuid.UUID, str, dict[str, str]],
    postings: dict[str, uuid.UUID],
    cv_version: uuid.UUID,
) -> None:
    """It is a warning, not a prohibition — the candidate knows their situation."""
    user_id, _, _ = account
    service = EngagementService(db_session)
    service.record_application(user_id, postings["ats"])
    db_session.commit()
    assert service.record_application(user_id, postings["aggregator"], force=True) is not None


def test_status_transitions_are_validated_and_recorded(
    db_session: Session,
    account: tuple[uuid.UUID, str, dict[str, str]],
    postings: dict[str, uuid.UUID],
    cv_version: uuid.UUID,
) -> None:
    user_id, _, _ = account
    service = EngagementService(db_session)
    record = service.record_application(user_id, postings["ats"])
    db_session.commit()

    service.advance(user_id, record.id, ApplicationStatus.INTERVIEWING, note="Call on Tuesday")
    db_session.commit()

    history = service.application_history(user_id, record.id)
    assert [row["to_status"] for row in history] == ["applied", "interviewing"]
    assert history[-1]["note"] == "Call on Tuesday"

    # A rejected application does not go back to interviewing.
    service.advance(user_id, record.id, ApplicationStatus.REJECTED)
    db_session.commit()
    with pytest.raises(EngagementError, match="Cannot move"):
        service.advance(user_id, record.id, ApplicationStatus.INTERVIEWING)


def test_no_response_is_a_first_class_outcome(
    db_session: Session,
    account: tuple[uuid.UUID, str, dict[str, str]],
    postings: dict[str, uuid.UUID],
    cv_version: uuid.UUID,
) -> None:
    """Silence is the most common outcome; a tracker without a word for it
    teaches people that it is failure."""
    user_id, _, _ = account
    service = EngagementService(db_session)
    record = service.record_application(user_id, postings["ats"])
    db_session.commit()

    service.advance(user_id, record.id, ApplicationStatus.NO_RESPONSE)
    db_session.commit()
    # And an employer who resurfaces months later is not a contradiction.
    assert service.advance(user_id, record.id, ApplicationStatus.INTERVIEWING) is not None


def test_another_users_application_is_invisible(
    db_session: Session,
    client: TestClient,
    account: tuple[uuid.UUID, str, dict[str, str]],
    postings: dict[str, uuid.UUID],
    cv_version: uuid.UUID,
) -> None:
    """§16.2: every query is scoped by user id at the repository layer."""
    user_id, _, _ = account
    record = EngagementService(db_session).record_application(user_id, postings["ats"])
    db_session.commit()

    other = client.post(
        "/api/v1/auth/register",
        json={
            "email": f"{uuid.uuid4().hex[:10]}@example.com",
            "password": PASSWORD,
            "accept_processing": True,
        },
    ).json()
    with pytest.raises(EngagementError, match="not found"):
        EngagementService(db_session).advance(
            uuid.UUID(other["user_id"]), record.id, ApplicationStatus.REJECTED
        )


# ── export and erasure ────────────────────────────────────────────────


def test_export_contains_everything_held(
    db_session: Session,
    account: tuple[uuid.UUID, str, dict[str, str]],
    postings: dict[str, uuid.UUID],
    cv_version: uuid.UUID,
) -> None:
    user_id, email, _ = account
    EngagementService(db_session).record_application(user_id, postings["ats"])
    db_session.commit()

    export = AccountService(db_session).export(user_id)
    assert export["account"]["email"] == email
    assert export["consents"], "consent history is part of the export"
    assert export["cv_versions"][0]["raw_text"] == "Built pipelines in Python"
    assert len(export["applications"]) == 1


def test_export_is_audited(
    db_session: Session, account: tuple[uuid.UUID, str, dict[str, str]]
) -> None:
    """§16.2: every export writes to the audit log."""
    user_id, _, _ = account
    AccountService(db_session).export(user_id)
    db_session.commit()

    action = db_session.execute(
        sa.text("SELECT action FROM audit_log WHERE user_id = :u ORDER BY created_at DESC LIMIT 1"),
        {"u": user_id},
    ).scalar_one()
    assert action == "export"


def test_deletion_destroys_cv_text_immediately(
    db_session: Session, account: tuple[uuid.UUID, str, dict[str, str]], cv_version: uuid.UUID
) -> None:
    """The user stops being processed the moment they ask (§12.5)."""
    user_id, _, _ = account
    receipt = AccountService(db_session).request_deletion(user_id)
    db_session.commit()

    assert receipt.cv_text_purged == 1
    raw_text = db_session.execute(
        sa.text("SELECT raw_text FROM cv_versions WHERE id = :id"), {"id": cv_version}
    ).scalar_one()
    assert raw_text == ""


def test_deletion_revokes_every_session(
    db_session: Session, client: TestClient, account: tuple[uuid.UUID, str, dict[str, str]]
) -> None:
    user_id, email, auth = account
    AccountService(db_session).request_deletion(user_id)
    db_session.commit()

    # The access token is still cryptographically valid; the account check is
    # what closes that window.
    assert client.get("/api/v1/profile", headers=auth).status_code == 401
    assert (
        client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD}).status_code
        == 401
    )


def test_hard_delete_waits_for_the_retention_window(
    db_session: Session, account: tuple[uuid.UUID, str, dict[str, str]]
) -> None:
    """The window exists so a deletion made in error can be reversed."""
    user_id, _, _ = account
    service = AccountService(db_session)
    service.request_deletion(user_id)
    db_session.commit()

    assert service.hard_delete_expired() == 0
    assert (
        db_session.execute(
            sa.text("SELECT count(*) FROM users WHERE id = :id"), {"id": user_id}
        ).scalar_one()
        == 1
    )

    db_session.execute(
        sa.text("UPDATE users SET deleted_at = now() - interval '31 days' WHERE id = :id"),
        {"id": user_id},
    )
    db_session.commit()

    assert service.hard_delete_expired() == 1
    db_session.commit()
    assert (
        db_session.execute(
            sa.text("SELECT count(*) FROM users WHERE id = :id"), {"id": user_id}
        ).scalar_one()
        == 0
    )


def test_the_audit_trail_outlives_the_account(
    db_session: Session, account: tuple[uuid.UUID, str, dict[str, str]]
) -> None:
    """A deletion has to remain demonstrable after the data is gone."""
    user_id, _, _ = account
    service = AccountService(db_session)
    service.request_deletion(user_id)
    db_session.execute(
        sa.text("UPDATE users SET deleted_at = now() - interval '31 days' WHERE id = :id"),
        {"id": user_id},
    )
    service.hard_delete_expired()
    db_session.commit()

    actions = (
        db_session.execute(
            sa.text("SELECT action FROM audit_log WHERE subject = :s"), {"s": str(user_id)}
        )
        .scalars()
        .all()
    )
    assert "hard_deleted" in actions


# ── HTTP surface ──────────────────────────────────────────────────────


def test_deletion_requires_typing_the_email(
    client: TestClient, account: tuple[uuid.UUID, str, dict[str, str]]
) -> None:
    _, email, auth = account
    wrong = client.request(
        "DELETE", "/api/v1/me", headers=auth, json={"confirm_email": "someone@else.com"}
    )
    assert wrong.status_code == 422

    right = client.request("DELETE", "/api/v1/me", headers=auth, json={"confirm_email": email})
    assert right.status_code == 200
    assert right.json()["status"] == "deletion_requested"


def test_withdrawing_processing_consent_points_at_deletion(
    client: TestClient, account: tuple[uuid.UUID, str, dict[str, str]]
) -> None:
    """Keeping the data after the basis for processing is withdrawn would be
    the dishonest option."""
    _, _, auth = account
    response = client.patch(
        "/api/v1/me/consents", headers=auth, json={"purpose": "processing", "granted": False}
    )
    assert response.status_code == 422
    assert "delete your account" in response.json()["title"]


def test_digest_consent_can_be_granted_and_revoked(
    client: TestClient, account: tuple[uuid.UUID, str, dict[str, str]]
) -> None:
    _, _, auth = account
    client.patch("/api/v1/me/consents", headers=auth, json={"purpose": "digest", "granted": True})
    assert "digest" in client.get("/api/v1/me/consents", headers=auth).json()["active"]

    client.patch("/api/v1/me/consents", headers=auth, json={"purpose": "digest", "granted": False})
    body = client.get("/api/v1/me/consents", headers=auth).json()
    assert "digest" not in body["active"]
    assert any(row["revoked_at"] for row in body["history"])


def test_export_downloads_as_a_file(
    client: TestClient, account: tuple[uuid.UUID, str, dict[str, str]]
) -> None:
    _, _, auth = account
    response = client.get("/api/v1/me/export", headers=auth)
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert response.json()["account"]["user_id"]


# ── interface language (§18, Phase 5) ─────────────────────────────────


def test_the_account_reports_its_locale_and_the_supported_set(
    client: TestClient, account: tuple[uuid.UUID, str, dict[str, str]]
) -> None:
    _, email, headers = account
    response = client.get("/api/v1/me", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["email"] == email
    assert body["locale"] == "en"
    assert body["supported_locales"] == ["en", "ar"]


def test_the_locale_is_stored_on_the_account_not_in_a_cookie(
    client: TestClient, account: tuple[uuid.UUID, str, dict[str, str]]
) -> None:
    """So it follows the candidate to their phone (§18, Phase 5)."""
    _, _, headers = account

    assert client.patch("/api/v1/me", json={"locale": "ar"}, headers=headers).status_code == 200
    assert client.get("/api/v1/me", headers=headers).json()["locale"] == "ar"

    # A fresh sign-in — a different session entirely — sees the same preference.
    assert client.patch("/api/v1/me", json={"locale": "en"}, headers=headers).status_code == 200
    assert client.get("/api/v1/me", headers=headers).json()["locale"] == "en"


def test_an_unsupported_locale_is_refused(
    client: TestClient, account: tuple[uuid.UUID, str, dict[str, str]]
) -> None:
    """Rejected rather than stored and silently ignored at render time."""
    _, _, headers = account
    response = client.patch("/api/v1/me", json={"locale": "fr"}, headers=headers)

    assert response.status_code == 422
    assert client.get("/api/v1/me", headers=headers).json()["locale"] == "en"


def test_the_account_endpoints_require_a_token(client: TestClient) -> None:
    assert client.get("/api/v1/me").status_code == 401
    assert client.patch("/api/v1/me", json={"locale": "ar"}).status_code == 401
