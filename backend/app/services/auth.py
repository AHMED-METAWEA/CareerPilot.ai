"""Registration, login and token rotation (§12.1, §16.2).

Three properties this module is responsible for:

* **Registration records consent with its policy version** (§11.1 step 1). A
  consent record that does not say what was agreed to is not a consent record.
* **Refresh tokens rotate and are single-use.** Presenting one issues a new pair
  and revokes the old. Reuse of a revoked token means a copy exists, so the
  whole family is revoked rather than the request merely refused.
* **Login does not reveal whether an email is registered.** The same response
  and roughly the same timing for an unknown user as for a wrong password.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import jwt
import structlog
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.session import rows_affected
from app.domain.identity import (
    TokenKind,
    TokenPair,
    expiry,
    new_refresh_token,
    validate_email,
    validate_password,
)

log = structlog.get_logger(__name__)

# Tuned for a 4 vCPU ARM host (§15.1): ~64 MB and two iterations lands near
# 50 ms per hash, which is expensive to attack and unnoticeable to a user.
_hasher = PasswordHasher(time_cost=2, memory_cost=65536, parallelism=2)

JWT_ALGORITHM = "HS256"
POLICY_VERSION = "2026-09-09"
"""The privacy policy these consents were given against. Bump it when the policy
changes; §16.4 makes re-consent a launch requirement, not a silent migration."""


class AuthError(Exception):
    """Registration or authentication failed. The message is safe to return."""


class InvalidCredentials(AuthError):
    def __init__(self) -> None:
        super().__init__("Email or password is incorrect")


class WeakPassword(AuthError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    user_id: uuid.UUID
    email: str


class AuthService:
    def __init__(self, session: Session, *, secret: str) -> None:
        if not secret or secret == "change-me-in-production":
            log.warning("auth.default_secret_in_use", detail="set ADMIN_TOKEN/JWT secret")
        self.session = session
        self.secret = secret

    # ── registration ─────────────────────────────────────────────────

    def register(
        self,
        email: str,
        password: str,
        *,
        locale: str = "en",
        consents: tuple[str, ...] = ("processing",),
    ) -> AuthenticatedUser:
        email = email.strip().lower()
        if not validate_email(email):
            raise AuthError("That does not look like an email address")

        problems = validate_password(password, email=email)
        if problems:
            raise WeakPassword([problem.message for problem in problems])

        existing = self.session.execute(
            text("SELECT 1 FROM users WHERE email = :email"), {"email": email}
        ).first()
        if existing:
            # Registration cannot be a membership oracle either.
            raise AuthError("That email cannot be registered")

        user_id = self.session.execute(
            text(
                "INSERT INTO users (email, password_hash, locale) "
                "VALUES (:email, :hash, :locale) RETURNING id"
            ),
            {"email": email, "hash": _hasher.hash(password), "locale": locale},
        ).scalar_one()

        for purpose in consents:
            self.record_consent(uuid.UUID(str(user_id)), purpose)

        log.info("auth.registered", user_id=str(user_id), consents=list(consents))
        return AuthenticatedUser(user_id=uuid.UUID(str(user_id)), email=email)

    def record_consent(self, user_id: uuid.UUID, purpose: str) -> None:
        """A consent record states what was agreed to, and under which policy."""
        self.session.execute(
            text(
                """
                INSERT INTO consents (user_id, purpose, policy_version, granted_at)
                VALUES (:user_id, :purpose, :version, now())
                """
            ),
            {"user_id": user_id, "purpose": purpose, "version": POLICY_VERSION},
        )

    def revoke_consent(self, user_id: uuid.UUID, purpose: str) -> int:
        result = self.session.execute(
            text(
                """
                UPDATE consents SET revoked_at = now()
                 WHERE user_id = :user_id AND purpose = :purpose AND revoked_at IS NULL
                """
            ),
            {"user_id": user_id, "purpose": purpose},
        )
        return rows_affected(result)

    def has_consent(self, user_id: uuid.UUID, purpose: str) -> bool:
        return (
            self.session.execute(
                text(
                    """
                    SELECT 1 FROM consents
                     WHERE user_id = :user_id AND purpose = :purpose AND revoked_at IS NULL
                     LIMIT 1
                    """
                ),
                {"user_id": user_id, "purpose": purpose},
            ).first()
            is not None
        )

    # ── login ────────────────────────────────────────────────────────

    def authenticate(self, email: str, password: str) -> AuthenticatedUser:
        row = self.session.execute(
            text(
                "SELECT id, email, password_hash FROM users "
                "WHERE email = :email AND deleted_at IS NULL"
            ),
            {"email": email.strip().lower()},
        ).first()

        if row is None:
            # Hash anyway: an unknown email must not return faster than a wrong
            # password, or the timing is a membership oracle.
            _hasher.hash(password)
            raise InvalidCredentials()

        try:
            _hasher.verify(row.password_hash, password)
        except (VerifyMismatchError, InvalidHashError) as exc:
            raise InvalidCredentials() from exc

        if _hasher.check_needs_rehash(row.password_hash):
            # Parameters were raised since this user last logged in.
            self.session.execute(
                text("UPDATE users SET password_hash = :hash WHERE id = :id"),
                {"hash": _hasher.hash(password), "id": row.id},
            )

        return AuthenticatedUser(user_id=uuid.UUID(str(row.id)), email=row.email)

    # ── tokens ───────────────────────────────────────────────────────

    def issue_tokens(self, user: AuthenticatedUser, *, family: str | None = None) -> TokenPair:
        now = datetime.now(UTC)
        access_expires = expiry(TokenKind.ACCESS, now=now)
        refresh_expires = expiry(TokenKind.REFRESH, now=now)

        access_token = jwt.encode(
            {
                "sub": str(user.user_id),
                "email": user.email,
                "iat": int(now.timestamp()),
                "exp": int(access_expires.timestamp()),
                "typ": TokenKind.ACCESS.value,
                # Without a unique id, two tokens issued in the same second for
                # the same user are byte-identical: sessions become
                # indistinguishable, and there is nothing to deny-list later.
                "jti": uuid.uuid4().hex,
            },
            self.secret,
            algorithm=JWT_ALGORITHM,
        )

        refresh_token = new_refresh_token()
        self.session.execute(
            text(
                """
                INSERT INTO refresh_tokens (user_id, token_hash, family, expires_at)
                VALUES (:user_id, :token_hash, :family, :expires_at)
                """
            ),
            {
                "user_id": user.user_id,
                "token_hash": _hash_token(refresh_token),
                # A family ties a rotation chain together, so detecting reuse can
                # revoke every descendant rather than one token.
                "family": family or uuid.uuid4().hex,
                "expires_at": refresh_expires,
            },
        )
        return TokenPair(
            access_token=access_token,
            refresh_token=refresh_token,
            access_expires_at=access_expires,
            refresh_expires_at=refresh_expires,
        )

    def verify_access_token(self, token: str) -> AuthenticatedUser:
        try:
            claims = jwt.decode(token, self.secret, algorithms=[JWT_ALGORITHM])
        except jwt.ExpiredSignatureError as exc:
            raise AuthError("Access token has expired") from exc
        except jwt.InvalidTokenError as exc:
            raise AuthError("Invalid access token") from exc

        if claims.get("typ") != TokenKind.ACCESS.value:
            # A refresh token must not be usable as an access token.
            raise AuthError("Invalid access token")
        return AuthenticatedUser(
            user_id=uuid.UUID(str(claims["sub"])), email=str(claims.get("email", ""))
        )

    def rotate(self, refresh_token: str) -> TokenPair:
        """Exchange a refresh token for a new pair, single use.

        Reuse of an already-rotated token is treated as compromise: the entire
        family is revoked, which logs out the attacker and the legitimate holder
        both. That is the intended outcome — one of them is not the user.
        """
        token_hash = _hash_token(refresh_token)
        row = self.session.execute(
            text(
                """
                SELECT r.id, r.user_id, r.family, r.revoked_at, r.expires_at, u.email
                  FROM refresh_tokens r
                  JOIN users u ON u.id = r.user_id
                 WHERE r.token_hash = :token_hash AND u.deleted_at IS NULL
                """
            ),
            {"token_hash": token_hash},
        ).first()

        if row is None:
            raise AuthError("Invalid refresh token")

        if row.revoked_at is not None:
            self.session.execute(
                text(
                    "UPDATE refresh_tokens SET revoked_at = now() "
                    "WHERE family = :family AND revoked_at IS NULL"
                ),
                {"family": row.family},
            )
            log.warning("auth.refresh_token_reuse", user_id=str(row.user_id), family=row.family)
            raise AuthError("Refresh token has already been used")

        if row.expires_at <= datetime.now(UTC):
            raise AuthError("Refresh token has expired")

        self.session.execute(
            text("UPDATE refresh_tokens SET revoked_at = now() WHERE id = :id"), {"id": row.id}
        )
        return self.issue_tokens(
            AuthenticatedUser(user_id=uuid.UUID(str(row.user_id)), email=row.email),
            family=row.family,
        )

    def revoke_refresh_token(self, refresh_token: str) -> bool:
        result = self.session.execute(
            text(
                "UPDATE refresh_tokens SET revoked_at = now() "
                "WHERE token_hash = :token_hash AND revoked_at IS NULL"
            ),
            {"token_hash": _hash_token(refresh_token)},
        )
        return rows_affected(result) > 0

    def revoke_all(self, user_id: uuid.UUID) -> int:
        result = self.session.execute(
            text(
                "UPDATE refresh_tokens SET revoked_at = now() "
                "WHERE user_id = :user_id AND revoked_at IS NULL"
            ),
            {"user_id": user_id},
        )
        return rows_affected(result)


def _hash_token(token: str) -> str:
    """Refresh tokens are stored hashed.

    A database copy should not hand over live sessions. SHA-256 is right here
    rather than Argon2: the token is 48 bytes of entropy, so there is nothing to
    brute-force, and rotation must stay fast.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
