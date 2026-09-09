"""Password and token policy (§16.2).

Pure decisions about credentials, kept out of the service layer so they can be
tested without a database and changed without touching request handling.

* **Argon2id** for password hashing, at parameters chosen for a 4 vCPU ARM host:
  strong enough to make offline cracking expensive, cheap enough that a login on
  the free tier does not take a second.
* **Short access tokens, rotating refresh tokens.** 15 minutes and 30 days
  (§16.2). A refresh token is single-use: presenting one issues a new pair and
  invalidates the old, so a stolen token is detectable — reuse of a rotated
  token means someone has a copy.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

ACCESS_TOKEN_TTL = timedelta(minutes=15)
REFRESH_TOKEN_TTL = timedelta(days=30)

MIN_PASSWORD_LENGTH = 12
"""Length beats composition rules. A 12-character passphrase resists guessing
better than eight characters tortured into four character classes, and the
rules are what push people toward Password1!"""

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


class TokenKind(StrEnum):
    ACCESS = "access"
    REFRESH = "refresh"


@dataclass(frozen=True, slots=True)
class PasswordProblem:
    code: str
    message: str


def validate_password(password: str, *, email: str | None = None) -> list[PasswordProblem]:
    """Reject what is actually dangerous, and nothing else."""
    problems: list[PasswordProblem] = []

    if len(password) < MIN_PASSWORD_LENGTH:
        problems.append(
            PasswordProblem(
                "too_short",
                f"Use at least {MIN_PASSWORD_LENGTH} characters. A short phrase works well.",
            )
        )
    if password.strip() != password:
        problems.append(
            PasswordProblem("surrounding_whitespace", "Remove the leading or trailing spaces.")
        )
    if email and password.casefold() == email.casefold():
        problems.append(
            PasswordProblem("same_as_email", "Your password cannot be your email address.")
        )
    if password.casefold() in _COMMON_PASSWORDS:
        problems.append(
            PasswordProblem("common_password", "This password appears in breach lists.")
        )
    return problems


_COMMON_PASSWORDS = frozenset(
    {
        "password123456",
        "123456789012",
        "qwertyuiopas",
        "passwordpassword",
        "administrator",
        "letmeinplease",
        "careerpilot123",
    }
)


def validate_email(email: str) -> bool:
    return bool(_EMAIL.match(email.strip())) and len(email) <= 254


def new_refresh_token() -> str:
    """A refresh token is an opaque secret, not a claims document.

    Nothing reads its contents, so there is nothing to be gained from making it
    a JWT — and an opaque token can be revoked by deleting a row.
    """
    return secrets.token_urlsafe(48)


@dataclass(frozen=True, slots=True)
class TokenPair:
    access_token: str
    refresh_token: str
    access_expires_at: datetime
    refresh_expires_at: datetime
    token_type: str = "bearer"

    def as_dict(self) -> dict[str, object]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_type": self.token_type,
            "expires_in": int((self.access_expires_at - datetime.now(UTC)).total_seconds()),
        }


def expiry(kind: TokenKind, *, now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    return now + (ACCESS_TOKEN_TTL if kind is TokenKind.ACCESS else REFRESH_TOKEN_TTL)
