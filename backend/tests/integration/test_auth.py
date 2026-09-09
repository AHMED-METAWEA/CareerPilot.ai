"""Registration, login, token rotation and consent (§12.1, §16.2).

The tests that matter here are the ones about what an attacker cannot do.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from app.services.auth import (
    POLICY_VERSION,
    AuthError,
    AuthService,
    InvalidCredentials,
    WeakPassword,
)

pytestmark = pytest.mark.db

PASSWORD = "a-long-enough-passphrase"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def service(db_session: Session) -> AuthService:
    return AuthService(db_session, secret="test-secret-not-the-default")


def unique_email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.com"


# ── registration ──────────────────────────────────────────────────────


def test_registration_records_consent_with_its_policy_version(
    db_session: Session, service: AuthService
) -> None:
    """A consent record that does not say what was agreed to is not one."""
    user = service.register(unique_email(), PASSWORD, consents=("processing", "digest"))
    db_session.commit()

    rows = db_session.execute(
        sa.text("SELECT purpose, policy_version, revoked_at FROM consents WHERE user_id = :id"),
        {"id": user.user_id},
    ).all()
    assert {row.purpose for row in rows} == {"processing", "digest"}
    assert all(row.policy_version == POLICY_VERSION for row in rows)
    assert all(row.revoked_at is None for row in rows)


def test_password_is_hashed_with_argon2(db_session: Session, service: AuthService) -> None:
    user = service.register(unique_email(), PASSWORD)
    stored = db_session.execute(
        sa.text("SELECT password_hash FROM users WHERE id = :id"), {"id": user.user_id}
    ).scalar_one()
    assert stored.startswith("$argon2id$")
    assert PASSWORD not in stored


def test_short_passwords_are_refused_with_a_reason(service: AuthService) -> None:
    with pytest.raises(WeakPassword) as exc:
        service.register(unique_email(), "short")
    assert any("at least" in problem for problem in exc.value.problems)


def test_a_password_equal_to_the_email_is_refused(service: AuthService) -> None:
    email = unique_email()
    with pytest.raises(WeakPassword):
        service.register(email, email)


def test_registration_is_not_a_membership_oracle(service: AuthService) -> None:
    """Registering an existing address must not confirm it exists."""
    email = unique_email()
    service.register(email, PASSWORD)
    with pytest.raises(AuthError) as exc:
        service.register(email, PASSWORD)
    assert "cannot be registered" in str(exc.value)
    assert email not in str(exc.value)


# ── login ─────────────────────────────────────────────────────────────


def test_login_succeeds_with_the_right_password(service: AuthService) -> None:
    email = unique_email()
    registered = service.register(email, PASSWORD)
    assert service.authenticate(email.upper(), PASSWORD).user_id == registered.user_id


def test_wrong_password_and_unknown_email_are_indistinguishable(service: AuthService) -> None:
    email = unique_email()
    service.register(email, PASSWORD)

    with pytest.raises(InvalidCredentials) as wrong:
        service.authenticate(email, "not-the-right-passphrase")
    with pytest.raises(InvalidCredentials) as unknown:
        service.authenticate(unique_email(), PASSWORD)

    assert str(wrong.value) == str(unknown.value)


def test_a_deleted_account_cannot_log_in(db_session: Session, service: AuthService) -> None:
    email = unique_email()
    user = service.register(email, PASSWORD)
    db_session.execute(
        sa.text("UPDATE users SET deleted_at = now() WHERE id = :id"), {"id": user.user_id}
    )
    with pytest.raises(InvalidCredentials):
        service.authenticate(email, PASSWORD)


# ── tokens ────────────────────────────────────────────────────────────


def test_access_token_round_trip(service: AuthService) -> None:
    user = service.register(unique_email(), PASSWORD)
    tokens = service.issue_tokens(user)
    assert service.verify_access_token(tokens.access_token).user_id == user.user_id


def test_a_refresh_token_is_not_an_access_token(service: AuthService) -> None:
    """Otherwise a long-lived credential becomes a short-lived one."""
    user = service.register(unique_email(), PASSWORD)
    tokens = service.issue_tokens(user)
    with pytest.raises(AuthError):
        service.verify_access_token(tokens.refresh_token)


def test_a_token_signed_with_another_secret_is_rejected(
    db_session: Session, service: AuthService
) -> None:
    user = service.register(unique_email(), PASSWORD)
    forged = AuthService(db_session, secret="a-different-secret").issue_tokens(user)
    with pytest.raises(AuthError):
        service.verify_access_token(forged.access_token)


def test_refresh_tokens_are_stored_hashed(db_session: Session, service: AuthService) -> None:
    """A database copy must not hand over live sessions."""
    user = service.register(unique_email(), PASSWORD)
    tokens = service.issue_tokens(user)
    db_session.commit()

    stored = db_session.execute(
        sa.text("SELECT token_hash FROM refresh_tokens WHERE user_id = :id"), {"id": user.user_id}
    ).scalar_one()
    assert stored != tokens.refresh_token
    assert len(stored) == 64


def test_rotation_issues_a_new_pair_and_invalidates_the_old(
    db_session: Session, service: AuthService
) -> None:
    user = service.register(unique_email(), PASSWORD)
    first = service.issue_tokens(user)
    db_session.commit()

    second = service.rotate(first.refresh_token)
    db_session.commit()

    assert second.refresh_token != first.refresh_token
    assert service.verify_access_token(second.access_token).user_id == user.user_id
    with pytest.raises(AuthError, match="already been used"):
        service.rotate(first.refresh_token)


def test_reusing_a_rotated_token_revokes_the_whole_family(
    db_session: Session, service: AuthService
) -> None:
    """Reuse means a copy exists, and one of the two holders is not the user."""
    user = service.register(unique_email(), PASSWORD)
    first = service.issue_tokens(user)
    db_session.commit()
    second = service.rotate(first.refresh_token)
    db_session.commit()

    with pytest.raises(AuthError):
        service.rotate(first.refresh_token)  # the attacker, or the user replaying
    db_session.commit()

    # The legitimate holder is logged out too. That is the intended outcome.
    with pytest.raises(AuthError):
        service.rotate(second.refresh_token)


def test_an_unknown_refresh_token_is_rejected(service: AuthService) -> None:
    with pytest.raises(AuthError, match="Invalid refresh token"):
        service.rotate("not-a-real-token")


def test_revoke_all_ends_every_session(db_session: Session, service: AuthService) -> None:
    user = service.register(unique_email(), PASSWORD)
    tokens = [service.issue_tokens(user) for _ in range(3)]
    db_session.commit()

    assert service.revoke_all(user.user_id) == 3
    db_session.commit()
    for token in tokens:
        with pytest.raises(AuthError):
            service.rotate(token.refresh_token)


# ── consent ───────────────────────────────────────────────────────────


def test_consent_can_be_revoked_and_is_recorded(db_session: Session, service: AuthService) -> None:
    """§12.5 and §11.7: unsubscribing is honoured immediately and recorded."""
    user = service.register(unique_email(), PASSWORD, consents=("processing", "digest"))
    db_session.commit()

    assert service.has_consent(user.user_id, "digest")
    assert service.revoke_consent(user.user_id, "digest") == 1
    db_session.commit()

    assert not service.has_consent(user.user_id, "digest")
    assert service.has_consent(user.user_id, "processing")
    revoked_at = db_session.execute(
        sa.text("SELECT revoked_at FROM consents WHERE user_id = :id AND purpose = 'digest'"),
        {"id": user.user_id},
    ).scalar_one()
    assert revoked_at is not None


# ── HTTP surface ──────────────────────────────────────────────────────


def test_registration_requires_processing_consent(client: TestClient) -> None:
    """Nothing is processed without it (§16.3)."""
    response = client.post(
        "/api/v1/auth/register",
        json={"email": unique_email(), "password": PASSWORD, "accept_processing": False},
    )
    assert response.status_code == 422
    assert "consent" in response.json()["title"].casefold()


def test_register_login_refresh_over_http(client: TestClient) -> None:
    email = unique_email()
    registered = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "accept_processing": True},
    )
    assert registered.status_code == 201
    assert registered.json()["token_type"] == "bearer"

    logged_in = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert logged_in.status_code == 200

    refreshed = client.post(
        "/api/v1/auth/refresh", json={"refresh_token": logged_in.json()["refresh_token"]}
    )
    assert refreshed.status_code == 200
    assert refreshed.json()["access_token"] != logged_in.json()["access_token"]


def test_protected_endpoints_refuse_an_absent_token(client: TestClient) -> None:
    response = client.get("/api/v1/profile")
    assert response.status_code == 401
    assert response.headers.get("www-authenticate") == "Bearer"


def test_wrong_password_over_http_is_401(client: TestClient) -> None:
    email = unique_email()
    client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "accept_processing": True},
    )
    response = client.post("/api/v1/auth/login", json={"email": email, "password": "wrong-one"})
    assert response.status_code == 401
