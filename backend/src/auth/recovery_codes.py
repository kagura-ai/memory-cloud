"""Random, single-use MFA recovery codes. Only their hashes are persisted."""

import hashlib
import hmac
import re
import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import User

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
    remaining = [value for value in hashes if not hmac.compare_digest(value, digest)]
    if len(remaining) == len(hashes):
        return False
    user.totp_recovery_hashes = remaining
    return True


async def consume_login_code(db: AsyncSession, user_id: str, code: str) -> bool:
    user = await db.scalar(select(User).where(User.user_id == user_id).with_for_update())
    if not user or not user.totp_enabled or not take_locked_code(user, code):
        return False
    await db.commit()
    return True
