"""Terms-of-service acceptance (Issue #1665).

One setting, ``TERMS_VERSION``, names the current terms version. Empty means the
feature is off: :func:`current_terms_version` returns ``None`` and every caller
treats that as "nothing to enforce, nothing to record" — sign-in behaves exactly
as it did before #1665.

With a version set:

- a new OAuth account is created only when the sign-up carries that version
  (enforced in ``api/routes/auth.py`` before the signup gate runs);
- every sign-in that carries it, and the in-app re-acceptance, records a row in
  ``terms_acceptances`` plus a ``terms.accepted`` audit row — but only when the
  user's latest accepted version is not already the current one, so repeated
  sign-ins do not pile up identical rows;
- ``GET /auth/me`` reports ``terms_acceptance_required`` for a user whose latest
  accepted version differs, and the web UI asks them to accept.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from models.auth import AuditLog
from models.terms import TermsAcceptance, TermsAcceptanceSource
from utils.logger import get_logger

logger = get_logger(__name__)

TERMS_ACCEPTED_ACTION = "terms.accepted"


def current_terms_version() -> str | None:
    """The configured terms version, or ``None`` when the feature is off."""
    return get_settings().terms_version or None


@dataclass(frozen=True)
class RecordResult:
    """What :meth:`TermsService.record` did.

    Attributes:
        version: The version now on the user's newest row.
        recorded: False when that version was already the newest one, so no
            row (and no audit row) was written.
    """

    version: str
    recorded: bool


class TermsService:
    """Read and append a user's terms-acceptance history."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def latest_version(self, user_id: str) -> str | None:
        """The version on the user's newest acceptance row, or ``None``."""
        result = await self.db.execute(
            select(TermsAcceptance.version)
            .where(TermsAcceptance.user_id == user_id)
            .order_by(TermsAcceptance.accepted_at.desc(), TermsAcceptance.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def acceptance_required(self, user_id: str) -> bool:
        """True when a version is configured and the user has not accepted it."""
        current = current_terms_version()
        if current is None:
            return False
        return await self.latest_version(user_id) != current

    async def record(
        self,
        *,
        user_id: str,
        user_email: str,
        version: str,
        source: TermsAcceptanceSource,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> RecordResult:
        """Append an acceptance of ``version`` and its audit row, then commit.

        Idempotent per version: when ``version`` is already the user's newest
        accepted version nothing is written. The caller decides whether
        ``version`` is acceptable (it must be the current one); this method does
        not read the setting.

        The audit row names the version only — ``user_metadata`` never carries
        anything the browser sent besides it.
        """
        if await self.latest_version(user_id) == version:
            return RecordResult(version=version, recorded=False)

        # Client-side id so the audit row can name the acceptance before flush.
        acceptance = TermsAcceptance(
            id=uuid.uuid4(), user_id=user_id, version=version, source=source
        )
        self.db.add(acceptance)
        self.db.add(
            AuditLog(
                user_email=user_email,
                user_id=user_id,
                action=TERMS_ACCEPTED_ACTION,
                resource=f"terms_acceptance:{acceptance.id}",
                user_metadata={"version": version},
                ip_address=ip_address,
                user_agent=user_agent,
            )
        )
        await self.db.commit()
        logger.info("terms_accepted", user_id=user_id, version=version, source=source)
        return RecordResult(version=version, recorded=True)
