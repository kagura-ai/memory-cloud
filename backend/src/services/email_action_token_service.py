"""Single-use, expiring tokens delivered by email (Issue #1678).

Used for the password-reset link, the set-a-password link a signed-in user
requests, and email verification. The design follows the OWASP Forgot Password
cheat sheet:

- The raw token is ``secrets.token_urlsafe(32)`` (256 bits). Only its SHA-256
  hex digest is stored, so a database read cannot be replayed as a link.
- Issuing a token for a ``(user, purpose)`` invalidates that pair's outstanding
  tokens: only the newest link works.
- Consuming is one ``UPDATE ... WHERE used_at IS NULL AND expires_at > now
  RETURNING``. Under Postgres row locking a second, concurrent consume of the
  same token re-evaluates the predicate after the first commits and matches no
  row, so a link can never be used twice.
- Unknown, expired, used and malformed tokens are indistinguishable to the
  caller (``None``).

Neither the raw token nor a URL that embeds it is ever logged here.
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from models.auth import EmailActionToken
from utils.datetime import utcnow
from utils.hashing import sha256_hex
from utils.logger import get_logger
from utils.utf8 import is_utf8_encodable

logger = get_logger(__name__)

EmailActionPurpose = Literal["verify_email", "set_password", "reset_password"]

# ``token_urlsafe(32)`` yields 43 characters. Anything far outside that range
# was never issued; refusing it early keeps junk out of the hash + query.
_MIN_TOKEN_LENGTH = 32
_MAX_TOKEN_LENGTH = 128


@dataclass(frozen=True)
class IssuedEmailActionToken:
    """A freshly issued token.

    Attributes:
        raw_token: The secret to embed in the emailed link. **Sensitive** —
            never log it or return it in an API response.
        expires_at: Naive UTC expiry.
    """

    raw_token: str
    expires_at: datetime


@dataclass(frozen=True)
class ConsumedEmailActionToken:
    """What a successfully consumed token acts on.

    Attributes:
        user_id: The account the link was issued for.
        email: The address the link was sent to. Callers compare it with the
            account's current email and refuse on a mismatch.
    """

    user_id: str
    email: str


def token_ttl(purpose: EmailActionPurpose) -> timedelta:
    """Return the configured lifetime of a token for ``purpose``.

    Args:
        purpose: The token purpose.

    Returns:
        30 minutes for the password links and 24 hours for email verification
        by default (``PASSWORD_RESET_TOKEN_TTL_MINUTES``,
        ``SET_PASSWORD_TOKEN_TTL_MINUTES``, ``VERIFY_EMAIL_TOKEN_TTL_HOURS``).
    """
    settings = get_settings()
    if purpose == "reset_password":
        return timedelta(minutes=settings.password_reset_token_ttl_minutes)
    if purpose == "set_password":
        return timedelta(minutes=settings.set_password_token_ttl_minutes)
    return timedelta(hours=settings.verify_email_token_ttl_hours)


def _looks_issued(raw_token: str) -> bool:
    """Return whether ``raw_token`` could be a token this service issued."""
    return (
        isinstance(raw_token, str)
        and _MIN_TOKEN_LENGTH <= len(raw_token) <= _MAX_TOKEN_LENGTH
        and is_utf8_encodable(raw_token)
    )


class EmailActionTokenService:
    """Issue and consume email action tokens.

    The service only flushes; the caller owns the transaction. That lets a
    caller dispatch the email before committing and roll back on a failed
    send (the pre-commit dispatch pattern of #469), so a committed token is
    never stranded without its email.
    """

    def __init__(self, db: AsyncSession) -> None:
        """Bind the service to a session.

        Args:
            db: The request's async session.
        """
        self.db = db

    async def issue(
        self,
        *,
        user_id: str,
        email: str,
        purpose: EmailActionPurpose,
    ) -> IssuedEmailActionToken:
        """Issue a new token, invalidating the user's outstanding ones.

        Args:
            user_id: The account the link acts on.
            email: The address the link will be sent to.
            purpose: What the link lets its holder do.

        Returns:
            The raw token and its expiry. The row is flushed, not committed.
        """
        now = utcnow()
        await self.db.execute(
            update(EmailActionToken)
            .where(
                EmailActionToken.user_id == user_id,
                EmailActionToken.purpose == purpose,
                EmailActionToken.used_at.is_(None),
            )
            .values(used_at=now)
            .execution_options(synchronize_session=False)
        )
        raw_token = secrets.token_urlsafe(32)
        expires_at = now + token_ttl(purpose)
        self.db.add(
            EmailActionToken(
                user_id=user_id,
                purpose=purpose,
                token_hash=sha256_hex(raw_token),
                email=email,
                expires_at=expires_at,
            )
        )
        await self.db.flush()
        logger.info("email_action_token_issued", user_id=user_id, purpose=purpose)
        return IssuedEmailActionToken(raw_token=raw_token, expires_at=expires_at)

    async def consume(
        self,
        *,
        raw_token: str,
        purpose: EmailActionPurpose,
    ) -> ConsumedEmailActionToken | None:
        """Mark a token used, exactly once.

        Args:
            raw_token: The token from the link.
            purpose: The purpose the endpoint accepts. A token issued for
                another purpose does not match.

        Returns:
            The user and address the token was issued for, or ``None`` when
            the token is unknown, expired, already used, issued for another
            purpose, or malformed. The update is flushed, not committed.
        """
        if not _looks_issued(raw_token):
            return None
        token_hash = sha256_hex(raw_token)
        now = utcnow()
        result = await self.db.execute(
            update(EmailActionToken)
            .where(
                EmailActionToken.token_hash == token_hash,
                EmailActionToken.purpose == purpose,
                EmailActionToken.used_at.is_(None),
                EmailActionToken.expires_at > now,
            )
            .values(used_at=now)
            .returning(
                EmailActionToken.user_id,
                EmailActionToken.email,
                EmailActionToken.token_hash,
            )
            .execution_options(synchronize_session=False)
        )
        row = result.first()
        # The lookup is by digest, so there is no secret-dependent comparison
        # to time; the constant-time check below only guards against a
        # returned row that is not the one asked for.
        if row is None or not hmac.compare_digest(row.token_hash, token_hash):
            return None
        logger.info("email_action_token_consumed", user_id=row.user_id, purpose=purpose)
        return ConsumedEmailActionToken(user_id=row.user_id, email=row.email)
