"""Terms-of-service acceptance history (Issue #1665).

``terms_acceptances`` is append-only: one row per recorded acceptance, never
updated. A user's accepted version is the version on their newest row; there is
no copy of it on ``users``, so the history and the current answer cannot
disagree.

Rows are only written while the deployment names a current version
(``TERMS_VERSION``); with the setting empty the table stays untouched. See
``services/terms_service.py`` for when a row is written.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base

# Where an acceptance came from. Byte-identical to the CHECK below and to the
# e85 migration (schema drift test).
TermsAcceptanceSource = Literal["login", "join", "password", "reaccept"]
TERMS_ACCEPTANCE_SOURCES: tuple[TermsAcceptanceSource, ...] = (
    "login",
    "join",
    "password",
    "reaccept",
)


class TermsAcceptance(Base):
    """One recorded acceptance of one terms version by one user.

    Attributes:
        id: Primary key (UUID). The only identifier that appears in audit rows.
        user_id: The accepting user's ID (OAuth sub). ``ON DELETE CASCADE`` —
            the history goes with the account.
        version: The ``TERMS_VERSION`` string the user accepted.
        source: ``login`` (OAuth sign-in or sign-up), ``join`` (sign-up through
            a beta invite link), ``password`` (password sign-in) or
            ``reaccept`` (the in-app prompt after the version changed).
        accepted_at: When the row was written (timezone-aware).
    """

    __tablename__ = "terms_acceptances"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    user_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "source IN ('login', 'join', 'password', 'reaccept')",
            name="valid_terms_acceptance_source",
        ),
        # Serves "newest row for this user" (ORDER BY accepted_at DESC LIMIT 1).
        Index("ix_terms_acceptances_user_accepted_at", "user_id", "accepted_at"),
    )

    def __repr__(self) -> str:
        return f"<TermsAcceptance(user_id='{self.user_id}', version='{self.version}')>"
