"""Random, single-use MFA recovery codes. Only their hashes are persisted."""

import hashlib
import hmac
import re
import secrets
import time
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import AuditLog, User

_FORMAT = re.compile(r"(?:[0-9a-fA-F]{8}-){3}[0-9a-fA-F]{8}")


def issue_codes() -> tuple[list[str], list[str]]:
    codes = []
    for _ in range(8):
        raw = secrets.token_hex(16)
        codes.append("-".join(raw[i : i + 8] for i in range(0, 32, 8)))
    return codes, [_digest(code) for code in codes]


def is_recovery_code(code: str) -> bool:
    return bool(_FORMAT.fullmatch(code))


def _digest(code: str) -> str:
    return hashlib.sha256(code.lower().encode("ascii")).hexdigest()


def take_locked_code(user: User, code: str) -> bool:
    """Caller holds the users row lock through commit."""
    if not is_recovery_code(code):
        return False
    digest = _digest(code)
    hashes = user.totp_recovery_hashes or []
    remaining = [value for value in hashes if not _available(value, digest)]
    if len(remaining) == len(hashes):
        return False
    user.totp_recovery_hashes = remaining
    return True


@dataclass(frozen=True)
class RecoveryClaim:
    digest: str
    marker: str
    expires: int


def _available(value: str, digest: str) -> bool:
    """A crash leaves the reservation eligible for a new attempt after five minutes."""
    stored, _, reservation = value.partition(":")
    if not hmac.compare_digest(stored, digest):
        return False
    if not reservation:
        return True
    expiry, _, _nonce = reservation.partition(":")
    return expiry.isdecimal() and int(expiry) <= time.time()


async def _locked_account(db: AsyncSession, user_id: str) -> User | None:
    return await db.scalar(
        select(User)
        .where(User.user_id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def claim_login_code(db: AsyncSession, user_id: str, code: str) -> RecoveryClaim | None:
    """Reserve while the password session is opened without holding its user row lock."""
    if not is_recovery_code(code):
        return None
    user = await _locked_account(db, user_id)
    if not user or not user.totp_enabled:
        return None
    digest = _digest(code)
    hashes = user.totp_recovery_hashes or []
    for index, value in enumerate(hashes):
        if _available(value, digest):
            expires = int(time.time()) + 300
            claim = RecoveryClaim(digest, f"{digest}:{expires}:{secrets.token_hex(16)}", expires)
            user.totp_recovery_hashes = [*hashes[:index], claim.marker, *hashes[index + 1 :]]
            await db.commit()
            return claim
    return None


async def settle_login_code(
    db: AsyncSession, user_id: str, claim: RecoveryClaim, *, audit: AuditLog | None
) -> bool:
    """Consume with the sign-in audit, or restore only this reservation on failure.

    A different marker or an absent marker means the set rotated, MFA was disabled,
    or a newer attempt reclaimed the code. None of those writes can be undone here.
    """
    user = await _locked_account(db, user_id)
    if not user or not user.totp_enabled or claim.marker not in (user.totp_recovery_hashes or []):
        return False
    if audit is not None and claim.expires <= time.time():
        return False
    values = [value for value in user.totp_recovery_hashes or [] if value != claim.marker]
    if audit is None:
        values.append(claim.digest)
    else:
        db.add(audit)
    user.totp_recovery_hashes = values
    await db.commit()
    return True
